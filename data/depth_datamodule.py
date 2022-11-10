from pathlib import Path
import numpy as np
import pytorch_lightning as pl
import torch
from torch.utils.data import DataLoader
from torchvision import transforms as torch_transforms
import re
import json
from typing import *
from data.image_dataset import ImageDataset, SynchronizedTransform
from utils.image_utils import create_circular_mask

_mac_regex = re.compile(r'^(?!.*\._)')


class _RandomAffine:
    def __init__(self, degrees: Tuple[float, float], translate: Tuple[float, float]):
        self.degrees = degrees
        self.translate = translate

    def __call__(self, data: torch.Tensor, use_corner_as_fill: bool = False):
        border_color = torch.mean(data[:, [0, -1, 0, 1], [0, -1, 0, 1]], dim=-1)
        fill = border_color.tolist() if use_corner_as_fill else 0
        affine = torch_transforms.RandomAffine(degrees=self.degrees, translate=self.translate, fill=fill)
        return affine(data)


class _EndoMask:
    def __init__(self, mask_color: Union[float, List[float]] = None):
        """
        :param mask_color: color to use for mask. If left as none, a randomized dark color is used per image.
        """
        self.mask_color = mask_color

    def __call__(self, data: torch.Tensor):
        if self.mask_color is None:
            mask_color = torch.rand((3, 1), dtype=torch.float) / 10
        else:
            mask_color = self.mask_color
        data[:, create_circular_mask(*data.shape[-2:], invert=True)] = mask_color
        return data


class Squarify:
    def __init__(self, image_size: int = None):
        self.image_size = image_size
        self.resize = None
        if self.image_size is not None:
            self.resize = torch_transforms.Resize(self.image_size)

    def __call__(self, data: torch.Tensor):
        data = torch_transforms.CenterCrop(min(data.shape[-2:]))(data)
        if self.image_size is not None:
            data = self.resize(data)
        return data


class DepthDataModule(pl.LightningDataModule):
    def __init__(self,
                 batch_size,
                 color_image_directory: str,
                 depth_image_directory: str,
                 split: dict = None,
                 image_size: Tuple[int, int] = (256, 256)):
        """

        :param batch_size: batch sized to use for training
        :param color_image_directory: path to the color images. Will be searched recursively
        :param depth_image_directory: path to the depth images. Will be searched recursively
        :param split: can be one of the following:
            - dictionary of regex strings to filter the filenames with:
                {'train': ".*train.*", 'validate': ".*val.*", 'test': ".*test.*"} <- i.e. if divided into subfolders
            - dictionary of floats specifying the random data split:
                {'train': 0.5, 'validate': 0.3, 'test': 0.2}
            - A combination of the two previous options:
                {'train': 0.75, 'validate': 0.25, 'test': ".*model_01.*"}
                The files for any regex matches will be assigned first and the remainder will respect the float splits
            - string pointing to a file from a previously saved split
            defaults to the first example assuming subfolders with "train", "val", and "test"
        :param image_size: final image size to return for training.
        """
        super().__init__()
        self.save_hyperparameters("batch_size")

        self.batch_size = batch_size
        self.image_size = image_size
        if split is None:
            split = {'train': ".*train.*", 'validate': ".*val.*", 'test': ".*test.*"}

        if isinstance(split, str):
            self.split_files = json.load(split)
        else:
            image_files = {
                'color': [str(f) for f in Path(color_image_directory).rglob('*') if _mac_regex.search(str(f))],
                'depth': [str(f) for f in Path(depth_image_directory).rglob('*') if _mac_regex.search(str(f))]}
            assert len(image_files['color']) == len(image_files['depth'])
            image_files['color'].sort()
            image_files['depth'].sort()
            self.split_files = {'test': {}, 'validate': {}, 'train': {}}
            stages = list(self.split_files.keys())
            for stage in stages:
                if isinstance(split[stage], str):
                    test_re = re.compile(split[stage])
                    for key, file_list in image_files.items():
                        self.split_files[stage][key] = [s for s in file_list if test_re.search(s)]
                        for f in self.split_files[stage][key]:
                            file_list.remove(f)

            indices = np.random.permutation(np.linspace(0,
                                                        len(image_files['color']) - 1,
                                                        len(image_files['color']))).astype(int)
            remaining_count = len(indices)
            for stage in stages:
                if isinstance(split[stage], float):
                    stage_indices = indices[:int(split[stage] * remaining_count) + 1]
                    for key, file_list in image_files.items():
                        self.split_files[stage][key] = np.asarray(file_list)[stage_indices]
                    indices = indices[len(stage_indices):]

        self.data_train: ImageDataset = None
        self.data_val: ImageDataset = None
        self.data_test: ImageDataset = None

    def prepare_data(self):
        pass

    def setup(self, stage: str = None):
        normalize = torch_transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])  # imagenet
        color_mask = _EndoMask()
        depth_mask = _EndoMask(mask_color=0)
        squarify = Squarify(image_size=min(self.image_size))
        affine_transform = SynchronizedTransform(transform=_RandomAffine(degrees=(0, 360), translate=(.1, .1)),
                                                 num_synchros=2, additional_args=[[True], [False]])
        color_jitter = torch_transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1)
        color_transforms = torch_transforms.Compose([color_mask, squarify, color_jitter, affine_transform])
        depth_transforms = torch_transforms.Compose([depth_mask, squarify, affine_transform])

        # Assign train/val datasets for use in dataloaders
        if stage == "fit" or stage is None:
            self.data_train = ImageDataset(files=list(zip(self.split_files['train']['color'],
                                                          self.split_files['train']['depth'])),
                                           transforms=[color_transforms, depth_transforms])
            self.data_val = ImageDataset(files=list(zip(self.split_files['validate']['color'],
                                                        self.split_files['validate']['depth'])),
                                         transforms=[color_transforms, depth_transforms])
        if stage == "validate":
            self.data_val = ImageDataset(files=list(zip(self.split_files['validate']['color'],
                                                        self.split_files['validate']['depth'])),
                                         transforms=[color_transforms, depth_transforms])
        if stage == "test" or stage is None:
            self.data_test = ImageDataset(files=list(zip(self.split_files['test']['color'],
                                                         self.split_files['test']['depth'])),
                                          transforms=[color_transforms, depth_transforms])

    def train_dataloader(self):
        return DataLoader(self.data_train, batch_size=self.batch_size, num_workers=1, shuffle=True, pin_memory=True)

    def val_dataloader(self):
        return DataLoader(self.data_val, batch_size=self.batch_size, num_workers=1, shuffle=False, pin_memory=True)

    def test_dataloader(self):
        return DataLoader(self.data_test, batch_size=self.batch_size, num_workers=1, shuffle=False, pin_memory=True)


if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from utils.image_utils import matplotlib_show
    color_dir = r'/Users/peter/isys/output/color'
    depth_dir = r'/Users/peter/isys/output/depth'
    dm = DepthDataModule(batch_size=3,
                         color_image_directory=color_dir,
                         depth_image_directory=depth_dir,
                         split={'train': .6, 'validate': 0.4, 'test': ".*00015.*"})
    dm.setup('fit')
    loader = dm.train_dataloader()
    matplotlib_show(*next(iter(loader)))
    plt.show(block=True)