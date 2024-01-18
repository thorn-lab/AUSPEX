import copy
import warnings

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

import matplotlib.pyplot as plt

# initiate c lib for complex integral
lib = ctypes.CDLL(os.path.join(os.path.dirname(__file__), 'lib/int_lib.so'))
lib.f.restype = ctypes.c_double
lib.f.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.c_void_p)

warnings.filterwarnings("ignore", message="The integral is probably divergent, or slowly convergent.")

reflection_data = FileReader('/media/yui-local/Scratch/works/mtz_with_raw_img/mtz/5usx.mtz', 'mtz', None, None)


class NemoHandler(object):
    def __init__(self, reso_min=10.):
        """

        @param reso_min: Minimum resolution.
        """
        super(NemoHandler, self).__init__()
        self._reso_low = None
        self._sorted_arg = None
        self._reso_min = reso_min
        self._refl_data = None
        self._work_obs = None
        self._work_norma_obs = None
        self._centric_flag = None
        self._acentric_flag = None
        self._prob_c = None
        self._ind_final_outlier = None

    def refl_data_prepare(self, reflection_data, observation_label='FP'):
        self._refl_data = reflection_data
        self._work_obs = reflection_data.get_miller_array(observation_label)
        d_spacings = self._work_obs.d_spacings().data().as_numpy_array()
        self._sorted_arg = d_spacings.argsort()[::-1]
        self._reso_select = (d_spacings > self._reso_min).sum()
        self._reso_low = d_spacings[self._sorted_arg][:self._reso_select]
        self._obs_low = self._work_obs.data().as_numpy_array()[self._sorted_arg][:self._reso_select]
        self._sig_low = self._work_obs.sigmas().as_numpy_array()[self._sorted_arg][:self._reso_select]
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

    def outliers_by_wilson(self, prob_level=0.02):
        ac_obs = self._work_norma_obs.data().as_numpy_array()[self._acentric_ind_low]
        c_obs = self._work_norma_obs.data().as_numpy_array()[self._centric_ind_low]
        if self._work_norma_obs.is_xray_amplitude_array():
            self._prob_ac = cumprob_ac_amplitude(ac_obs)
            self._prob_c = cumprob_c_amplitude(c_obs)
            ac_outlier_flag = self._prob_ac < prob_level
            c_outlier_flag = self._prob_c < prob_level
        elif self._work_norma_obs.is_xray_intensity_array():
            ac_sigs = self._work_norma_obs.sigmas().as_numpy_array()[self._acentric_ind_low]
            c_sigs = self._work_norma_obs.sigmas().as_numpy_array()[self._centric_ind_low]
            self._prob_ac = cumprob_ac_intensity(ac_obs, ac_sigs)

            ac_outlier_flag = self._prob_ac < prob_level
            if c_obs.sum() == 0:  # no centric reflections
                self._prob_c = np.array([],dtype=float)
                c_outlier_flag = np.array([], dtype=bool)
            else:
                self._prob_c = cumprob_c_intensity(c_obs, c_sigs)
                c_outlier_flag = self._prob_c < prob_level
        else:
            raise Exception("Unknown observation type: {}".format(self._work_norma_obs.observation_type()))
        return ac_outlier_flag, c_outlier_flag

    def cluster_detect(self):
        ac_outlier_flag, c_outlier_flag = self.outliers_by_wilson(0.05)
        ac_weak = self._obs_low[self._acentric_flag][ac_outlier_flag]
        c_weak = self._obs_low[self._centric_flag][c_outlier_flag]
        d_ac_weak = 1/self._reso_low[self._acentric_flag][ac_outlier_flag]**2
        d_c_weak = 1/self._reso_low[self._centric_flag][c_outlier_flag]**2
        sig_ac_weak = self._sig_low[self._acentric_flag][ac_outlier_flag]
        sig_c_weak = self._sig_low[self._centric_flag][c_outlier_flag]

        ind_weak = np.concatenate((self._acentric_ind_low[ac_outlier_flag], self._centric_ind_low[c_outlier_flag]))
        weak_prob = np.concatenate([self._prob_ac[ac_outlier_flag], self._prob_c[c_outlier_flag]])

        if ind_weak.size == 1:
            # if only one outlier by wilson then we become conservative. level 0.02 -> 0.005
            conserv_ind_weak = ind_weak[ind_weak <= 0.005]
            return self._work_obs.indices().as_vec3_double().as_numpy_array()[conserv_ind_weak].astype(int)

        ind_weak_work = copy.deepcopy(ind_weak)[weak_prob <= 0.025]
        j = np.concatenate((ac_weak/sig_ac_weak, c_weak/sig_c_weak))
        #j = np.concatenate((ac_weak, c_weak))
        i = np.concatenate((d_ac_weak, d_c_weak))
        #sorted_args_weak = np.argsort(i)
        #pos_weak = np.vstack((i, j)).transpose()
        #p2_dist = norm(pos_weak[:, None] - pos_weak[None, :], axis=2)
        auspex_array = np.vstack((1./(self._reso_low**2), self._obs_low/self._sig_low)).transpose()
        #auspex_array = np.vstack((1. / (self._reso_low ** 2), self._obs_low)).transpose()
        # generate feature_array. weak obs will be labeled as 1, others 0.
        #feature_array = np.zeros(self._reso_low.size, dtype=int)
        #feature_array[np.isin(self._sorted_arg[:self._reso_select], ind_weak_work)] = np.arange(1, len(ind_weak_work) + 1)
        ind_cluster_by_size = []
        plt.scatter(auspex_array[:, 0], auspex_array[:, 1], s=3, alpha=0.5)
        plt.scatter(i[weak_prob <= 0.01], j[weak_prob <= 0.01], c='r', s=3, alpha=0.5)
        plt.savefig('/home/yui-local/test_img/{0}_{1}.png'.format(self._refl_data.file_name[-8:-4], "weak"))
        plt.clf()

        for num_points in range(ind_weak_work.size, 1, -1):
            #print(num_points)
            #detect = DBSCAN(eps=dist, min_samples=num_points)
            detect = HDBSCAN(min_cluster_size=num_points,
                             #min_samples=ind_weak_work.size-num_points+1,
                             #max_cluster_size=ind_weak_work.size,
                             algorithm='brute')
            try:
                auspex_array_for_fit = copy.deepcopy(auspex_array)
                auspex_array_for_fit[:, 0] = np.percentile(auspex_array_for_fit[:, 1],80) / auspex_array_for_fit[:, 0].max() * auspex_array [:, 0]
                cluster_fitted = detect.fit(auspex_array_for_fit)
            except KeyError:
                continue
            cluster_labels = cluster_fitted.labels_
            cluster_prob = cluster_fitted.probabilities_
            unique_cluster_label = np.unique(cluster_labels)
            unique_cluster_label = unique_cluster_label[unique_cluster_label >= 0]
            if unique_cluster_label.size == 0:
                continue
            else:
                # initiation
                #j_work = copy.deepcopy(j)
                #i_work = copy.deepcopy(i)
                in_token = np.empty(0, dtype=int)
                in_prob = np.empty(0, dtype=float)
                #print(unique_cluster_label)
                for c_label in unique_cluster_label:
                    args_ = np.argwhere((cluster_labels == c_label) & (cluster_prob >= 0.5)).flatten()
                    if args_.size == 0:
                        continue
                    ind_sub_cluster = self._sorted_arg[:self._reso_select][args_]
                    wilson_filter = np.isin(ind_sub_cluster, ind_weak_work)
                    #print(wilson_filter)
                    #in_token = np.append(in_token, ind_sub_cluster[wilson_filter])
                    #print(np.any(wilson_filter))
                    # plt.scatter(auspex_array[:, 0], auspex_array[:, 1], s=3, alpha=0.5)
                    # plt.scatter(auspex_array[args_, 0], auspex_array[args_, 1], s=3, alpha=0.5)
                    # plt.savefig('/home/yui-local/test_img/{0}_{1}_{2}.png'.format(self._refl_data.file_name[-8:-4], num_points, c_label))
                    # plt.clf()
                    if (wilson_filter.sum() / wilson_filter.size) > 0.5:
                        in_token = np.append(in_token, ind_sub_cluster)
                        in_prob = np.append(in_prob, cluster_prob[args_])
                #args_ = np.argwhere(cluster_prob >= 0.8).flatten()
                #in_token = self._sorted_arg[:self._reso_select][args_]
                #print(in_prob)
                #print(in_token)
                #j_in = self._work_obs.data().as_numpy_array()[in_token]
                #i_in = 1. / self._work_obs.d_spacings().data().as_numpy_array()[in_token]**2

                ind_cluster_by_size.append(np.unique(in_token))

        if not ind_cluster_by_size:
            # when no cluster can be found we need to be very conservative thus level 0.02->0.005
            conserv_ind_weak = ind_weak[weak_prob <= 0.005]
            return self._work_obs.indices().as_vec3_double().as_numpy_array()[conserv_ind_weak].astype(int)

        cluster_ind_recur, cluster_counts_recur = np.unique(np.concatenate(ind_cluster_by_size), return_counts=True)
        cluster_prob = cluster_counts_recur / len(ind_cluster_by_size)
        #print(ind_cluster_by_size)
        # print(cluster_counts_recur)
        if cluster_ind_recur.size == 0 or cluster_ind_recur.size == 1:
            # when the intersection of the cluster and wilson outliers has only one element,
            # we need to be very conservative thus level 0.02->0.005
            final_weak_ind = ind_weak[weak_prob <= 0.005]
        elif cluster_counts_recur.size == 2 and np.all(cluster_counts_recur == 1):
            # when the cluster only consists 2 elements and occurs only once,
            # we tend to be more careful thus level 0.02->0.005
            final_weak_ind = ind_weak[weak_prob <= 0.005]
        elif cluster_counts_recur.size == 2 and not np.all(cluster_counts_recur == 1):
            # when the cluster only consists 2 elements but occurs more than once,
            # we just kick out those occurring only once
            final_weak_ind = cluster_ind_recur[cluster_counts_recur > 1]
        elif np.all(cluster_counts_recur == cluster_counts_recur[0]) and np.all(cluster_counts_recur == 1):
            # when the cluster consists more than 2 elements and occurs only once,
            # set level slightly higher than 0.01
            # print(3)
            ind_weak_and_cluster = np.isin(cluster_ind_recur, ind_weak[weak_prob <= 0.011])
            final_weak_ind = cluster_ind_recur[ind_weak_and_cluster]
        elif np.all(cluster_counts_recur == cluster_counts_recur[0]) and not np.all(cluster_counts_recur == 1):
            # when the elements in the clusters are repetitive coherently. pass
            final_weak_ind = cluster_ind_recur
        else:
            # when the elements in the clusters are varying. only those with 0.8 occurrence rate will pass
            repetitive_ind = cluster_counts_recur >= np.min((cluster_counts_recur.max(), ind_weak_work.size)) * 0.8
            ind_weak_and_cluster = np.unique(
                np.concatenate((ind_weak[(weak_prob <= 0.02) & (i <= 0.002)], cluster_ind_recur[repetitive_ind]))
            )
            #ind_weak_and_cluster = np.isin(cluster_ind_recur, ind_weak[weak_prob <= 0.005])
            #final_weak_ind = cluster_ind_recur[ind_weak_and_cluster | repetitive_ind]
            #final_weak_ind = cluster_ind_recur[repetitive_ind]
            final_weak_ind = ind_weak_and_cluster

            plt.scatter(auspex_array[:, 0], auspex_array[:, 1], s=3, alpha=0.5)
            plt.scatter(1. / self._work_obs.d_spacings().data().as_numpy_array()[cluster_ind_recur] ** 2,
                        self._work_obs.data().as_numpy_array()[cluster_ind_recur]/self._work_obs.sigmas().as_numpy_array()[cluster_ind_recur]
                        , s=3,
                        alpha=0.5)  # cluster_ind_recur[repetitive_ind]
            plt.savefig('/home/yui-local/test_img/{0}_{1}.png'.format(self._refl_data.file_name[-8:-4], "cluster"))
            plt.clf()

        plt.scatter(auspex_array[:, 0], auspex_array[:,1],s=3,alpha=0.5)
        plt.scatter(1. / self._work_obs.d_spacings().data().as_numpy_array()[final_weak_ind]**2,
                    self._work_obs.data().as_numpy_array()[final_weak_ind]/self._work_obs.sigmas().as_numpy_array()[final_weak_ind]
                    ,s=3,alpha=0.5) #cluster_ind_recur[repetitive_ind]
        plt.savefig('/home/yui-local/test_img/{0}_{1}.png'.format(self._refl_data.file_name[-8:-4], "final"))
        plt.clf()

        return self._work_obs.indices().as_vec3_double().as_numpy_array()[final_weak_ind].astype(int)
        #return final_weak_ind


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
    return nquad(prob_c_intensity_integrand, [[0, np.inf], [-np.inf, e_square]], opts={"limit": 301})[0]
cumprob_c_intensity = np.vectorize(base_cumprob_c_intensity)


def construct_ih_table(obs, inv_res_sqr):
    obs_ext = obs[None, :] * np.ones(obs.size)[:, None]
    inv_res_sqr_ext = inv_res_sqr[None, :] * np.ones(inv_res_sqr.size)[:, None]
    obs_ih_table = delete_diag(obs_ext)
    inv_res_sqr_ih_table = delete_diag(inv_res_sqr_ext)
    return obs_ih_table, inv_res_sqr_ih_table

def delete_diag(square_matrix):
    # inspired by https://stackoverflow.com/questions/46736258/deleting-diagonal-elements-of-a-numpy-array
    # delete diagonal elements
    m = square_matrix.shape[0]
    s0, s1 = square_matrix.strides
    return np.lib.stride_tricks.as_strided(square_matrix.ravel()[1:], shape=(m-1, m), strides=(s0+s1, s1)).reshape(m, -1)
