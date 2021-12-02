''''''
'''
@Author: Huangying Zhan (huangying.zhan.work@gmail.com)
@Date: 2020-05-13
@Copyright: Copyright (C) Huangying Zhan 2020. All rights reserved. Please refer to the license file.
@LastEditTime: 2020-05-28
@LastEditors: Huangying Zhan
@Description: Dataset loaders for Adelaide Driving Sequence
'''

import numpy as np
from glob import glob
import os

from .dataset import Dataset
from libs.general.utils import *


class DevonIsland(Dataset):
    """Base class of dataset loaders for Adelaide Driving Sequence
    """

    def __init__(self, *args, **kwargs):
        super(DevonIsland, self).__init__(*args, **kwargs)
    
    ''' In general, you don't need to change this part ''' 
    def synchronize_timestamps(self):
        """Synchronize RGB, Depth, and Pose timestamps to form pairs
        
        Returns:
            a dictionary containing
                - **rgb_timestamp** : {'depth': depth_timestamp, 'pose': pose_timestamp}
        """
        self.rgb_d_pose_pair = {}
        len_seq = len(glob(os.path.join(self.data_dir['img'], "*.{}".format(self.cfg.image.ext))))
        for i in range(len_seq):
            self.rgb_d_pose_pair[i] = {}
            self.rgb_d_pose_pair[i]['depth'] = i
            self.rgb_d_pose_pair[i]['pose'] = i
    
    def get_timestamp(self, img_id):
        """Get timestamp for the query img_id

        Args:
            img_id (int): query image id

        Returns:
            timestamp (int): timestamp for query image
        """
        return img_id

    def save_result_traj(self, traj_txt, poses):
        """Save trajectory (absolute poses) as KITTI odometry file format

        Args:
            txt (str): pose text file path
            poses (dict): poses, each pose is a [4x4] array
        """
        global_poses_arr = convert_SE3_to_arr(poses)
        save_traj(traj_txt, global_poses_arr, format='kitti')

    ''' In general, you need to write the following functions for your own dataset''' 
    def get_intrinsics_param(self):
        """Read intrinsics parameters for each dataset

        Returns:
            intrinsics_param (list): [cx, cy, fx, fy]
        """
        # Reference image size
        # Camera one 

        self.height = 960
        self.width = 1280


        K = [1280.0-635.139038086, 960.0-463.537109375, 968.999694824, 968.999694824]

        '''
        if self.cfg.image.width:
          K[0] *= (self.cfg.image.width / self.width)
        
        if self.cfg.image.height
          K[1] *= (self.cfg.image.height / self.height)
        '''
        return K
    
    def get_data_dir(self):
        """Get data directory

        Returns:
            a dictionary containing
                - **img** (str) : image data directory
                - (optional) **depth** (str) : depth data direcotry or None
                - (optional) **depth_src** (str) : depth data type [gt/None]
        """
        data_dir = {"depth": None, "depth_src": None}

        # get image data directory
        img_seq_dir = os.path.join(
                            self.cfg.directory.img_seq_dir,
                            self.cfg.seq
                            )
        data_dir['img'] = os.path.join(img_seq_dir)
 
        return data_dir
    
    def get_image(self, timestamp):
        """Get image data given the image timestamp

        Args:
            timestamp (int): timestamp for the image
            
        Returns:
            img (array, [CxHxW]): image data
        """
        img_path = os.path.join(self.data_dir['img'],
                            "{:06d}.{}".format(timestamp + 33864, self.cfg.image.ext)
                            )
        img = read_image(img_path, self.cfg.image.height, self.cfg.image.width)

        return img
    
    ''' 
        These functions are not necessary to run DF-VO. 
        However, if you want to use RGB-D data, get_depth() is required.
        If you have gt poses as well for comparison, get_gt_poses() is required.
    ''' 
    def get_depth(self, timestamp):
        """Get GT/precomputed depth data given the timestamp

        Args:
            timestamp (int): timestamp for the depth

        Returns:
            depth (array, [HxW]): depth data
        """
        raise NotImplementedError

    def get_gt_poses(self):
        """Load ground-truth poses
        
        Returns:
            gt_poses (dict): each pose is a [4x4] array
        """
        raise NotImplementedError
    