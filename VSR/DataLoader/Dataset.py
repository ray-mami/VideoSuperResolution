"""
Copyright: Intel.Corp 2017-2018
Author: Wenyi Tang
Email: wenyi.tang@intel.com
Created Date: Jan. 11th 2018
Updated Date: May 24th 2018

offline dataset collector
support random crop
"""
import json
from pathlib import Path
from ..Util.Utility import to_list


class Dataset:
    """Dataset provides training/validation/testing data for neural network.

    This is a simple wrapper provides train, val, test and additional properties
    """

    def __init__(self, **kwargs):
        self._args = kwargs
        # default attr
        self._args['mode'] = 'pil-image1' if not 'mode' in kwargs else kwargs['mode']
        self._args['depth'] = 1 if not 'depth' in kwargs else kwargs['depth']

    def __getattr__(self, item):
        if item in self._args:
            return self._args[item]
        else:
            if item in ('train', 'val', 'test'):
                raise ValueError(f'un{item}able')
            return None

    def __setitem__(self, key, value):
        self._args[key] = value

    def setattr(self, **kwargs):
        for key in kwargs:
            self._args[key] = kwargs[key]


def _glob_absolute_pattern(url):
    url = Path(url)
    url_p = url
    while True:
        try:
            if url_p.exists():
                break
        except OSError:
            url_p = url_p.parent
            continue
        if url_p == url_p.parent:
            break
        url_p = url_p.parent
    url_r = url.relative_to(url_p)
    if str(url_r) == '.':
        return url_p.iterdir()
    return url_p.glob(str(url_r))


def load_datasets(json_file):
    """load dataset described in JSON file"""

    datasets = {}
    with open(json_file, 'r') as fd:
        config = json.load(fd)
        all_set_path = config["Path"]
        for name, value in config["Dataset"].items():
            assert isinstance(value, dict)
            datasets[name] = Dataset()
            for i in value:
                if not i in ('train', 'val', 'test'):
                    continue
                sets = []
                for j in to_list(value[i]):
                    try:
                        sets += list(_glob_absolute_pattern(all_set_path[j]))
                    except KeyError:
                        sets += list(_glob_absolute_pattern(j))
                datasets[name].__setitem__(i, sets)
            if 'param' in value:
                for k, v in value['param'].items():
                    datasets[name].__setitem__(k, v)
    return datasets
