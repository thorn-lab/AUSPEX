import copy

import numpy as np
from numpy.linalg import norm

import os, ctypes
from scipy import LowLevelCallable
from scipy.special import pbdv, erfc
from scipy.integrate import quad, nquad

from sklearn.cluster import HDBSCAN, DBSCAN

from cctbx.array_family import flex
from mmtbx.scaling import absolute_scaling

from ReflectionData import FileReader

# initiate c lib for complex integral
lib = ctypes.CDLL(os.path.join(os.path.dirname(__file__), 'lib/int_lib.so'))
lib.f.restype = ctypes.c_double
lib.f.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.c_void_p)

reflection_data = FileReader('/media/yui-local/Scratch/works/mtz_with_raw_img/mtz/5usx.mtz', 'mtz', None, None)


class NemoHandler(object):
    def __init__(self, reso_min=10.):
        super(NemoHandler, self).__init__()
        self._reso_low = None
        self._sorted_arg = None
        self._reso_min = reso_min
        self._refl_data = None
        self._work_obs = None
        self._work_norma_obs = None
        self._centric_flag = None
        self._acentric_flag = None
        self._ind_final_outlier = None

    def refl_data_prepare(self, reflection_data, observation_label='FP'):
        self._refl_data = reflection_data
        self._work_obs = reflection_data.get_miller_array(observation_label)
        d_spacings = self._work_obs.d_spacings().data().as_numpy_array()
        self._sorted_arg = d_spacings.argsort()[::-1]
        self._reso_select = (d_spacings > self._reso_min).sum()
        self._reso_low = reflection_data.resolution[self._sorted_arg][:self._reso_select]
        self._obs_low = self._work_obs.data().as_numpy_array()[self._sorted_arg][:self._reso_select]
        normalizer = absolute_scaling.kernel_normalisation(self._work_obs, auto_kernel=True)
        if self._work_obs.is_xray_amplitude_array():
            self._work_norma_obs = self._work_obs.customized_copy(
                data=flex.sqrt(normalizer.normalised_miller.data() / normalizer.normalised_miller.epsilons().data().as_double())
            )
        if self._work_obs.is_xray_intensity_array():
            self._work_norma_obs = self._work_obs.customized_copy(
                data=normalizer.normalised_miller.data() / normalizer.normalised_miller.epsilons().data().as_double(),
                sigmas=normalizer.normalised_miller.sigmas() / normalizer.normalised_miller.epsilons().data().as_double()
            )
        self._centric_flag = self._work_norma_obs.centric_flags().data().as_numpy_array()[self._sorted_arg][:self._reso_select]
        self._acentric_flag = ~self._work_norma_obs.centric_flags().data().as_numpy_array()[self._sorted_arg][:self._reso_select]
        self._centric_ind_low = self._sorted_arg[:self._reso_select][self._centric_flag]
        self._acentric_ind_low = self._sorted_arg[:self._reso_select][self._acentric_flag]

    def outliers_by_wilson(self, prob_level=0.01):
        ac_obs = self._work_norma_obs.data().as_numpy_array()[self._acentric_ind_low]
        c_obs = self._work_norma_obs.data().as_numpy_array()[self._centric_ind_low]
        if self._work_norma_obs.is_xray_amplitude_array():
            ac_outlier_flag = cumprob_ac_amplitude(ac_obs) < prob_level
            c_outlier_flag = cumprob_c_amplitude(c_obs) < prob_level
        elif self._work_norma_obs.is_xray_intensity_array():
            ac_sigs = self._work_norma_obs.sigmas().as_numpy_array()[self._acentric_ind_low]
            c_sigs = self._work_norma_obs.sigmas().as_numpy_array()[self._centric_ind_low]
            ac_outlier_flag = cumprob_ac_intensity(ac_obs, ac_sigs) < prob_level
            c_outlier_flag = cumprob_c_intensity(c_obs, c_sigs) < prob_level
        return ac_outlier_flag, c_outlier_flag

    def cluster_detect(self):
        ac_outlier_flag, c_outlier_flag = self.outliers_by_wilson(0.01)
        ac_weak = self._obs_low[self._acentric_flag][ac_outlier_flag]
        c_weak = self._obs_low[self._centric_flag][c_outlier_flag]
        d_ac_weak = 1/self._reso_low[self._acentric_flag][ac_outlier_flag]**2
        d_c_weak = 1/self._reso_low[self._centric_flag][c_outlier_flag]**2

        ind_weak = np.concatenate((self._acentric_ind_low[ac_outlier_flag], self._centric_ind_low[c_outlier_flag]))

        j = np.concatenate((ac_weak, c_weak))
        i = np.concatenate((d_ac_weak, d_c_weak))
        sorted_args_weak = np.argsort(i)
        pos_weak = np.vstack((i, j)).transpose()
        p2_dist = norm(pos_weak[:, None] - pos_weak[None, :], axis=2)
        auspex_array = np.vstack((1/(self._reso_low*self._reso_low), self._obs_low)).transpose()
        # generate feature_array. weak obs will be labeled as 1, others 0.
        #feature_array = np.zeros(self._reso_low.size, dtype=int)
        #feature_array[np.isin(self._sorted_arg[:self._reso_select], ind_weak)] = np.arange(1, len(ind_weak) + 1)

        unique_dist = np.unique(p2_dist)
        if unique_dist.size > 21:
            unique_dist = np.linspace(unique_dist[0], unique_dist[-1], ind_weak.size)
        for dist in unique_dist[1:]:
            for num_points in range(ind_weak.size + 1, 2, -1):
                print(dist)
                print(num_points)
                #detect = DBSCAN(eps=dist, min_samples=num_points)

                detect = HDBSCAN(min_cluster_size=num_points,
                                 max_cluster_size=ind_weak.size,
                                 cluster_selection_epsilon=dist,
                                 allow_single_cluster=True)
                try:
                    cluster_fitted = detect.fit(auspex_array)
                except KeyError:
                    continue
                cluster_labels = cluster_fitted.labels_
                cluster_prob = cluster_fitted.probabilities_
                unique_cluster_label = np.unique(cluster_labels)
                unique_cluster_label = unique_cluster_label[unique_cluster_label >= 0]
                if unique_cluster_label.size == 0:
                    continue
                if (cluster_prob > 1.).sum() > ind_weak.size:
                    continue
                #elif (cluster_labels == 0).sum() > ind_weak.size:
                #    continue
                else:
                    # initiation
                    ind_weak_work = copy.deepcopy(ind_weak)
                    j_work = copy.deepcopy(j)
                    i_work = copy.deepcopy(i)
                    in_token = np.empty(0, dtype=int)
                    #print(unique_cluster_label)
                    for c_label in unique_cluster_label:
                        args_ = np.argwhere(cluster_labels == c_label).flatten()
                        ind_sub_cluster = self._sorted_arg[:self._reso_select][args_]
                        wilson_filter = np.isin(ind_sub_cluster, ind_weak_work)
                        #print(wilson_filter)
                        #in_token = np.append(in_token, ind_sub_cluster[wilson_filter])
                        #print(np.any(wilson_filter))
                        if np.all(wilson_filter):
                            in_token = np.append(in_token, ind_sub_cluster)
                    #args_ = np.argwhere(cluster_prob >= 0.8).flatten()
                    #in_token = self._sorted_arg[:self._reso_select][args_]

                    j_in = self._work_obs.data().as_numpy_array()[in_token]
                    i_in = 1. / self._work_obs.d_spacings().data().as_numpy_array()[in_token]**2

                    print(in_token)
                    ind_cluster = np.unique(in_token)
                    ind_cluster_work = copy.deepcopy(ind_cluster)
                    if ind_cluster.size <= 1:
                        continue
                    #print(self._work_obs.data().as_numpy_array()[np.sort(ind_cluster)])
                    #print(self._work_obs.d_spacings().data().as_numpy_array()[np.sort(ind_cluster)])
                    while not np.array_equal(np.sort(ind_cluster_work), np.sort(ind_weak_work)):
                        #print(self._work_obs.data().as_numpy_array()[np.sort(ind_weak_work)])
                        #print(self._work_obs.data().as_numpy_array()[np.sort(ind_cluster_work)])
                        #print(self._work_obs.d_spacings().data().as_numpy_array()[np.sort(ind_weak_work)])
                        #print(self._work_obs.d_spacings().data().as_numpy_array()[np.sort(ind_cluster_work)])
                        if ind_weak_work.size > ind_cluster.size:
                            j_work, i_work, ind_weak_work = self.remove_idx(j_work, i_work, ind_weak_work)
                        elif ind_weak_work.size < ind_cluster.size:
                            j_in, i_in, ind_cluster_work = self.remove_idx(j_in, i_in, ind_cluster_work)
                        else:
                            j_work, i_work, ind_weak_work = self.remove_idx(j_work, i_work, ind_weak_work)
                            j_in, i_in, ind_cluster_work = self.remove_idx(j_in, i_in, ind_cluster_work)
                        if (ind_weak_work.size <= 1) or (ind_cluster_work.size <= 1):
                            break
                        #continue
                    else:
                        self._ind_final_outlier = copy.deepcopy(ind_weak_work)
                        return self._work_obs.indices().as_vec3_double().as_numpy_array()[ind_weak_work]
    @staticmethod
    def remove_idx(j, i, ind):
        j_table, i_table = construct_ih_table(j, i)
        mean_angular_momentum = cal_mean_angular_momentum(j_table, i_table)
        #print(mean_angular_momentum[np.argsort(ind)])
        remove_idx = np.argmin(mean_angular_momentum)
        ind = np.delete(ind, remove_idx)
        j = np.delete(j, remove_idx)
        i = np.delete(i, remove_idx)
        return j, i, ind


def cumprob_c_amplitude(e):
    # probability of normalised centric amplitude smaller than e, given read e and sigma sig.
    # READ Acta. Cryst. (1999). D55, 1759-1764
    return 1. - erfc(e / 1.4142)


def cumprob_ac_amplitude(e):
    # probability of normalised acentric amplitude smaller than e, given read e and sigma sig.
    # READ Acta. Cryst. (1999). D55, 1759-1764
    return 1 - np.exp(-e*e)


def cumprob_ac_intensity(e_square, sig):
    # probability of normalised acentric intensity smaller than e**2, given read e**2 and sigma sig.
    # READ Acta. Cryst. (2016). D72, 375-387
    return 0.5 * (erfc(-e_square / 1.4142 / sig) - np.exp((sig - 2 * e_square) / 2) * erfc((sig - e_square) / 1.4142 / sig))

# def prob_c_intensity(e_square, sig):
#     # probability Baysian denominator for centric intensity, given read e**2 and sigma sig.
#     # READ Acta. Cryst. (2016). D72, 375-387
#     # equation 9b. analytical definite integral
#     # fast but intolerant to small values
#     p = 0.5 / np.sqrt(np.pi * sig) * np.exp(1 / 16 * (sig * sig - 4 * e_square - 4 * e_square * e_square / (sig * sig))) * \
#          pbdv(-0.5, 0.5 * sig - e_square / sig)[0]
#     return p

# def prob_c_intensity_integrand(x, e_square, sig):
#     # equation 9b. integrand
#     return 1/np.sqrt(2*np.pi*sig*sig)*np.exp(-0.5*np.square(e_square-x)/(sig*sig))/np.sqrt(2*np.pi*x)*np.exp(-0.5*x)

# def cumprob_c_intensity(e_square, sig):
#     # equation 21a. double numerical integral.
#     # very slow.
#     return nquad(prob_c_intensity_integrand, [[0, np.inf], [-np.inf, e_square]], args=(sig,))[0]

# def prob_c_intensity(e_square, sig):
#     # equation 9a. numerical integral.
#     return quad(prob_c_intensity_integrand, 0, np.inf, args=(e_square, sig))[0]

# def cumprob_c_intensity(e_square, sig):
#     # probability of centric normalised intensity smaller than e**2, given read e**2 and sigma sig.
#     # READ Acta. Cryst. (2016). D72, 375-387
#     # equation 21a. numerical integral.
#     # slowest.
#     return quad(prob_c_intensity, -np.inf, e_square, args=(sig,))[0]


def base_cumprob_c_intensity(e_square, sig):
    # probability of centric normalised intensity smaller than e**2, given read e**2 and sigma sig.
    # READ Acta. Cryst. (2016). D72, 375-387
    # based on low level c, faster
    c = ctypes.c_double(sig)
    user_data = ctypes.cast(ctypes.pointer(c), ctypes.c_void_p)
    prob_c_intensity_integrand = LowLevelCallable(lib.f, user_data)
    return nquad(prob_c_intensity_integrand, [[0, np.inf], [-np.inf, e_square]])[0]
cumprob_c_intensity = np.vectorize(base_cumprob_c_intensity)


def construct_ih_table(obs, inv_res_sqr):
    obs_ext = obs[None, :] * np.ones(obs.size)[:, None]
    inv_res_sqr_ext = obs[None, :] * np.ones(inv_res_sqr.size)[:, None]
    obs_ih_table = delete_diag(obs_ext)
    inv_res_sqr_ih_table = delete_diag(inv_res_sqr_ext)
    return obs_ih_table, inv_res_sqr_ih_table


def delete_diag(square_matrix):
    # inspired by https://stackoverflow.com/questions/46736258/deleting-diagonal-elements-of-a-numpy-array
    # delete diagonal elements
    m = square_matrix.shape[0]
    s0, s1 = square_matrix.strides
    return np.lib.stride_tricks.as_strided(square_matrix.ravel()[1:], shape=(m-1, m), strides=(s0+s1, s1)).reshape(m, -1)

def cal_mean_angular_momentum(obs, inv_res_sqr, axis=1):
    tot_angular_momentum = 2*np.pi*np.sum(obs*obs*inv_res_sqr*inv_res_sqr, axis=axis)
    tot_inertia = np.sum(inv_res_sqr*inv_res_sqr, axis=axis) * inv_res_sqr.size
    return tot_angular_momentum / tot_inertia