#copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

import os
import numpy as np
import glob

from paddlex.interpret.as_data_reader.readers import read_image
import paddlex.utils.logging as logging
from . import lime_base
from ._session_preparation import compute_features_for_kmeans, h_pre_models_kmeans


def load_kmeans_model(fname):
    import pickle
    with open(fname, 'rb') as f:
        kmeans_model = pickle.load(f)

    return kmeans_model


def combine_normlime_and_lime(lime_weights, g_weights):
    pred_labels = lime_weights.keys()
    combined_weights = {y: [] for y in pred_labels}

    for y in pred_labels:
        normlized_lime_weights_y = lime_weights[y]
        lime_weights_dict = {tuple_w[0]: tuple_w[1] for tuple_w in normlized_lime_weights_y}

        normlized_g_weight_y = g_weights[y]
        normlime_weights_dict = {tuple_w[0]: tuple_w[1] for tuple_w in normlized_g_weight_y}

        combined_weights[y] = [
            (seg_k, lime_weights_dict[seg_k] * normlime_weights_dict[seg_k])
            for seg_k in lime_weights_dict.keys()
        ]

        combined_weights[y] = sorted(combined_weights[y],
                                     key=lambda x: np.abs(x[1]), reverse=True)

    return combined_weights


def avg_using_superpixels(features, segments):
    one_list = np.zeros((len(np.unique(segments)), features.shape[2]))
    for x in np.unique(segments):
        one_list[x] = np.mean(features[segments == x], axis=0)

    return one_list


def centroid_using_superpixels(features, segments):
    from skimage.measure import regionprops
    regions = regionprops(segments + 1)
    one_list = np.zeros((len(np.unique(segments)), features.shape[2]))
    for i, r in enumerate(regions):
        one_list[i] = features[int(r.centroid[0] + 0.5), int(r.centroid[1] + 0.5), :]
    # print(one_list.shape)
    return one_list


def get_feature_for_kmeans(feature_map, segments):
    from sklearn.preprocessing import normalize
    centroid_feature = centroid_using_superpixels(feature_map, segments)
    avg_feature = avg_using_superpixels(feature_map, segments)
    x = np.concatenate((centroid_feature, avg_feature), axis=-1)
    x = normalize(x)
    return x


def precompute_normlime_weights(list_data_, predict_fn, num_samples=3000, batch_size=50, save_dir='./tmp'):
    # save lime weights and kmeans cluster labels
    precompute_lime_weights(list_data_, predict_fn, num_samples, batch_size, save_dir)

    # load precomputed results, compute normlime weights and save.
    fname_list = glob.glob(os.path.join(save_dir, f'lime_weights_s{num_samples}*.npy'))
    return compute_normlime_weights(fname_list, save_dir, num_samples)


def save_one_lime_predict_and_kmean_labels(lime_all_weights, image_pred_labels, cluster_labels, save_path):

    lime_weights = {}
    for label in image_pred_labels:
        lime_weights[label] = lime_all_weights[label]

    for_normlime_weights = {
        'lime_weights': lime_weights,  # a dict: class_label: (seg_label, weight)
        'cluster': cluster_labels  # a list with segments as indices.
    }

    np.save(save_path, for_normlime_weights)


def precompute_lime_weights(list_data_, predict_fn, num_samples, batch_size, save_dir):
    kmeans_model = load_kmeans_model(h_pre_models_kmeans)

    for data_index, each_data_ in enumerate(list_data_):
        if isinstance(each_data_, str):
            save_path = f"lime_weights_s{num_samples}_{each_data_.split('/')[-1].split('.')[0]}.npy"
            save_path = os.path.join(save_dir, save_path)
        else:
            save_path = f"lime_weights_s{num_samples}_{data_index}.npy"
            save_path = os.path.join(save_dir, save_path)

        if os.path.exists(save_path):
            logging.info(save_path + ' exists, not computing this one.', use_color=True)
            continue

        logging.info('processing'+each_data_ if isinstance(each_data_, str) else data_index + \
              f'+{data_index}/{len(list_data_)}', use_color=True)

        image_show = read_image(each_data_)
        result = predict_fn(image_show)
        result = result[0]  # only one image here.

        if abs(np.sum(result) - 1.0) > 1e-4:
            # softmax
            exp_result = np.exp(result)
            probability = exp_result / np.sum(exp_result)
        else:
            probability = result

        pred_label = np.argsort(probability)[::-1]

        # top_k = argmin(top_n) > threshold
        threshold = 0.05
        top_k = 0
        for l in pred_label:
            if probability[l] < threshold or top_k == 5:
                break
            top_k += 1

        if top_k == 0:
            top_k = 1

        pred_label = pred_label[:top_k]

        algo = lime_base.LimeImageInterpreter()
        interpreter = algo.interpret_instance(image_show[0], predict_fn, pred_label, 0,
                                          num_samples=num_samples, batch_size=batch_size)

        X = get_feature_for_kmeans(compute_features_for_kmeans(image_show).transpose((1, 2, 0)), interpreter.segments)
        try:
            cluster_labels = kmeans_model.predict(X)
        except AttributeError:
            from sklearn.metrics import pairwise_distances_argmin_min
            cluster_labels, _ = pairwise_distances_argmin_min(X, kmeans_model.cluster_centers_)
        save_one_lime_predict_and_kmean_labels(
            interpreter.local_weights, pred_label,
            cluster_labels,
            save_path
        )


def compute_normlime_weights(a_list_lime_fnames, save_dir, lime_num_samples):
    normlime_weights_all_labels = {}
    for f in a_list_lime_fnames:
        try:
            lime_weights_and_cluster = np.load(f, allow_pickle=True).item()
            lime_weights = lime_weights_and_cluster['lime_weights']
            cluster = lime_weights_and_cluster['cluster']
        except:
            print('When loading precomputed LIME result, skipping', f)
            continue
        print('Loading precomputed LIME result,', f)

        pred_labels = lime_weights.keys()
        for y in pred_labels:
            normlime_weights = normlime_weights_all_labels.get(y, {})
            w_f_y = [abs(w[1]) for w in lime_weights[y]]
            w_f_y_l1norm = sum(w_f_y)

            for w in lime_weights[y]:
                seg_label = w[0]
                weight = w[1] * w[1] / w_f_y_l1norm
                a = normlime_weights.get(cluster[seg_label], [])
                a.append(weight)
                normlime_weights[cluster[seg_label]] = a

            normlime_weights_all_labels[y] = normlime_weights

    # compute normlime
    for y in normlime_weights_all_labels:
        normlime_weights = normlime_weights_all_labels.get(y, {})
        for k in normlime_weights:
            normlime_weights[k] = sum(normlime_weights[k]) / len(normlime_weights[k])

    # check normlime
    if len(normlime_weights_all_labels.keys()) < max(normlime_weights_all_labels.keys()) + 1:
        print(
            "\n"
            "Warning: !!! \n"
            f"There are at least {max(normlime_weights_all_labels.keys()) + 1} classes, "
            f"but the NormLIME has results of only {len(normlime_weights_all_labels.keys())} classes. \n"
            "It may have cause unstable results in the later computation"
            " but can be improved by computing more test samples."
            "\n"
        )

    n = 0
    f_out = f'normlime_weights_s{lime_num_samples}_samples_{len(a_list_lime_fnames)}-{n}.npy'
    while os.path.exists(
            os.path.join(save_dir, f_out)
    ):
        n += 1
        f_out = f'normlime_weights_s{lime_num_samples}_samples_{len(a_list_lime_fnames)}-{n}.npy'
        continue

    np.save(
        os.path.join(save_dir, f_out),
        normlime_weights_all_labels
    )
    return os.path.join(save_dir, f_out)

