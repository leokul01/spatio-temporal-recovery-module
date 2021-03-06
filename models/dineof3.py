import os
import sys
from functools import reduce
import numpy as np
from scipy.sparse.linalg import svds
import tensorly as tl
import tensorly.decomposition as tld
from tensorly.decomposition.candecomp_parafac import initialize_factors
from tensorly.tenalg import khatri_rao
from tensorly.kruskal_tensor import (kruskal_normalise, KruskalTensor)
from sklearn.base import BaseEstimator
file_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.dirname(file_dir))
import utils


class DINEOF3(BaseEstimator):
    def __init__(self, R, tensor_shape,
                 decomp_type='HOOI', td_iter_max=100,
                 nitemax=300, toliter=1e-3, tol=1e-8, to_center=True,
                 keep_non_negative_only=True,
                 with_energy=True,
                 lat_lon_sep_centering=True):
        self.R = R
        self.decomp_type = decomp_type
        self.td_iter_max = td_iter_max
        self.nitemax = nitemax
        self.toliter = toliter
        self.tol = tol
        self.to_center = to_center
        self.keep_non_negative_only = keep_non_negative_only
        self.tensor_shape = tensor_shape
        self.with_energy = with_energy
        self.lat_lon_sep_centering = lat_lon_sep_centering

    def score(self, X, y):
        y_hat = self.predict(X)
        return -utils.nrmse(y_hat, y)

    def predict(self, X):
        output = np.array([self.reconstructed_tensor[x[0], x[1], x[2]] for x in X.astype(np.int)])
        return output

    def fit(self, X, y):
        tensor = utils.tensorify(X, y, self.tensor_shape)
        self._fit(tensor)

    def _fit(self, tensor):
        if self.to_center:
            tensor, *means = utils.center_3d_tensor(tensor, 
                                                    lat_lon_separately=self.lat_lon_sep_centering)

        # Initial guess
        nan_mask = np.isnan(tensor)
        tensor[nan_mask] = 0

        conv_error = 0
        energy_per_iter = []
        for i in range(self.nitemax):
            if self.decomp_type == 'HOOI':
                G, A = tld.partial_tucker(tensor,
                                          modes=list(range(len(self.R))),
                                          ranks=self.R,
                                          tol=self.tol,
                                          n_iter_max=self.td_iter_max)
            elif self.decomp_type == 'truncHOSVD':
                G, A = self.trunc_hosvd(tensor)
            elif self.decomp_type == 'PARAFAC':
                G, A = self.parafac(tensor, self.R, 
                                    n_iter_max=self.td_iter_max, 
                                    tol=self.tol)
            else:
                raise Exception(f'{self.decomp_type} is unsupported.')

            # Save energy characteristics for this iteration
            if self.with_energy:
                energy_i = self.calculate_energy(tensor, G, A)
                energy_per_iter.append(energy_i)

            tensor_hat = self.recontruct_tensor_by_factors(G, A)
            tensor_hat[~nan_mask] = tensor[~nan_mask]
            diff_in_clouds = tensor_hat[nan_mask] - tensor[nan_mask]
            new_conv_error = np.linalg.norm(diff_in_clouds) / np.std(tensor[~nan_mask])
            tensor = tensor_hat
            if (new_conv_error <= self.toliter) or (abs(new_conv_error - conv_error) < self.toliter):
                break
            conv_error = new_conv_error

        energy_per_iter = np.array(energy_per_iter)

        if self.to_center:
            tensor = utils.decenter_3d_tensor(tensor, *means, 
                                              lat_lon_separately=self.lat_lon_sep_centering)

        if self.keep_non_negative_only:
            tensor[tensor < 0] = 0

        # Save energies in model for distinct components (lat, lon, t)
        if self.with_energy:
            for i in range(tensor.ndim):
                setattr(self, f'total_energy_{i}', np.array(energy_per_iter[:, i, 0]))
                setattr(self, f'explained_energy_{i}', np.array(energy_per_iter[:, i, 1]))
                setattr(self, f'explained_energy_ratio_{i}', np.array(energy_per_iter[:, i, 2]))

        self.final_iter = i
        self.conv_error = conv_error
        self.reconstructed_tensor = tensor
        self.core_tensor = G
        self.factors = A

    def recontruct_tensor_by_factors(self, G, A):
        if self.decomp_type == 'PARAFAC':
            # Polyadic decomposition
            # G is a weights 1D array in this case
            tensor_hat = np.zeros(self.tensor_shape)
            for i, lmbda in enumerate(G):
                vecs = [factor[:, i] for factor in A]
                tensor_hat += lmbda * reduce(lambda x, y: np.multiply.outer(x, y), vecs)
        else:
            # Tucker decomposition
            # G is a core tensor
            tensor_hat = tl.tenalg.multi_mode_dot(G, A)

        return tensor_hat

    def parafac(self, tensor, rank, n_iter_max=100, tol=1e-8):
        factors = initialize_factors(tensor, rank)
        rec_errors = []
        norm_tensor = tl.norm(tensor, 2)

        for iteration in range(n_iter_max):
            for mode in range(tl.ndim(tensor)):
                # No reverse of factors, because tensorly's unfold works different
                # First frontal slice:
                # array([[ 1,  2,  3,  4],
                #        [ 5,  6,  7,  8],
                #        [ 9, 10, 11, 12]])
                #
                # Second frontal slice:
                # array([[13, 14, 15, 16],
                #        [17, 18, 19, 20],
                #        [21, 22, 23, 24]])
                #
                # 0th unfolding:
                # array([[ 1, 13,  2, 14,  3, 15,  4, 16],
                #        [ 5, 17,  6, 18,  7, 19,  8, 20],
                #        [ 9, 21, 10, 22, 11, 23, 12, 24]])
                mode_factors = [f for i, f in enumerate(factors) if i != mode]
                mode_sq_factors = [f.T @ f for f in mode_factors]
                unfold = tl.unfold(tensor, mode)
                m1 = khatri_rao(mode_factors)
                # Fix for tensorly's singular trouble
                m2 = np.linalg.pinv(reduce(lambda x, y: x * y, mode_sq_factors))
                factor = unfold @ m1 @ m2
                factors[mode] = factor

            rec_error = tl.norm(tensor - tl.kruskal_to_tensor((None, factors)), order=2)
            rec_error = rec_error / norm_tensor
            rec_errors.append(rec_error)

            if iteration >= 1:
                rec_error_decrease = abs(rec_errors[-2] - rec_errors[-1])
                stop_flag = rec_error_decrease < tol
                
                if stop_flag:
                    break

        return kruskal_normalise(KruskalTensor((None, factors)))

    def trunc_hosvd(self, tensor):
        A = []
        for i in range(tensor.ndim):
            unfold_i = tl.unfold(tensor, i)
            u, _, _ = svds(unfold_i, k=self.R[i], tol=self.tol)
            A.append(u)
        A = np.array(A)
        G = tl.tenalg.multi_mode_dot(tensor, A, transpose=True)
        return G, A

    def calculate_energy(self, tensor, G, A):
        if self.decomp_type == 'PARAFAC':
            raise Exception('Energy for PARAFAC is not implemented.')
        else:
            return utils.calculate_tucker_energy(tensor, A)
