''''''
'''
@Author: Huangying Zhan (huangying.zhan.work@gmail.com)
@Date: 2019-01-01
@Copyright: Copyright (C) Huangying Zhan 2020. All rights reserved. Please refer to the license file.
@LastEditTime: 2020-07-09
@LastEditors: Huangying Zhan
@Description: DF-VO core program
'''

import cv2
import copy
from glob import glob
import math
from matplotlib import pyplot as plt
import numpy as np
import os
from time import time
from tqdm import tqdm

from libs.geometry.camera_modules import SE3
import libs.datasets as Dataset
# from libs.deep_models.deep_models import DeepModel
from libs.general.frame_drawer import FrameDrawer
from libs.general.timer import Timer
from libs.matching.keypoint_sampler import KeypointSampler
from libs.matching.depth_consistency import DepthConsistency
from libs.tracker import EssTracker, PnpTracker
from libs.general.utils import *



class DFVO():
    def __init__(self, cfg):
        """
        Args:
            cfg (edict): configuration reading from yaml file
        """
        # configuration
        self.cfg = cfg

        # tracking stage
        self.tracking_stage = 0

        # predicted global poses
        self.global_poses = {0: SE3()}

        # reference data and current data
        self.initialize_data()

        self.setup()

    def setup(self):
        """Reading configuration and setup, including

            - Timer
            - Dataset
            - Tracking method
            - Keypoint Sampler
            - Deep networks
            - Deep layers
            - Visualizer
        """
        # timer
        self.timers = Timer()

        # intialize dataset
        self.dataset = Dataset.datasets["devon_island"](self.cfg)

        # get tracking method
        self.tracking_method = self.cfg.tracking_method
        self.initialize_tracker()

        # initialize keypoint sampler
        self.kp_sampler = KeypointSampler(self.cfg)
        
        # Depth consistency
        if self.cfg.kp_selection.depth_consistency.enable:
            self.depth_consistency_computer = DepthConsistency(self.cfg, self.dataset.cam_intrinsics)

        # visualization interface
        self.drawer = FrameDrawer(self.cfg.visualization)
        
    def initialize_data(self):
        """initialize data of current view and reference view
        """
        self.ref_data = {}
        self.cur_data = {}

    def initialize_tracker(self):
        """Initialize tracker
        """
        if self.tracking_method == 'hybrid':
            self.e_tracker = EssTracker(self.cfg, self.dataset.cam_intrinsics, self.timers)
            self.pnp_tracker = PnpTracker(self.cfg, self.dataset.cam_intrinsics)
        elif self.tracking_method == 'PnP':
            self.pnp_tracker = PnpTracker(self.cfg, self.dataset.cam_intrinsics)
        elif self.tracking_method == 'deep_pose':
            return
        else:
            assert False, "Wrong tracker is selected, choose from [hybrid, PnP, deep_pose]"

    def update_global_pose(self, new_pose, scale=1.):
        """update estimated poses w.r.t global coordinate system

        Args:
            new_pose (SE3): new pose
            scale (float): scaling factor
        """
        self.cur_data['pose'].t = self.cur_data['pose'].R @ new_pose.t * scale \
                            + self.cur_data['pose'].t
        self.cur_data['pose'].R = self.cur_data['pose'].R @ new_pose.R
        self.global_poses[self.cur_data['id']] = copy.deepcopy(self.cur_data['pose'])

        # define T

        # print them as requested in superglue
        # self.curr_data['id'], self.ref_data['id'] 0, 0, K1, K2, T

    def tracking(self):
        """Tracking using both Essential matrix and PnP
        Essential matrix for rotation and translation direction;
            *** triangluate depth v.s. CNN-depth for translation scale ***
        PnP if Essential matrix fails
        """
        # First frame
        if self.tracking_stage == 0:
            # initial pose
            if self.cfg.directory.gt_pose_dir is not None:
                self.cur_data['pose'] = SE3(self.dataset.gt_poses[self.cur_data['id']])
            else:
                self.cur_data['pose'] = SE3()
            return

        # Second to last frames
        elif self.tracking_stage >= 1:
            ''' keypoint selection '''
            if self.tracking_method in ['hybrid', 'PnP']:
                # Depth consistency (CNN depths + CNN pose)
                if self.cfg.kp_selection.depth_consistency.enable:
                    self.depth_consistency_computer.compute(self.cur_data, self.ref_data)

                # kp_selection
                self.timers.start('kp_sel', 'tracking')
                kp_sel_outputs = self.kp_sampler.kp_selection(self.cur_data, self.ref_data)
                if kp_sel_outputs['good_kp_found']:
                    self.kp_sampler.update_kp_data(self.cur_data, self.ref_data, kp_sel_outputs)
                self.timers.end('kp_sel')

            ''' Pose estimation '''
            # Initialize hybrid pose
            hybrid_pose = SE3()
            E_pose = SE3()

            if not(kp_sel_outputs['good_kp_found']):
                print("No enough good keypoints, constant motion will be used!")
                pose = self.ref_data['motion']
                self.update_global_pose(pose, 1)
                return 


            ''' E-tracker --> I must to work here ''' 
            if self.tracking_method in ['hybrid']:
                # Essential matrix pose
                self.timers.start('E-tracker', 'tracking')
                e_tracker_outputs = self.e_tracker.compute_pose_2d2d(
                                self.ref_data[self.cfg.e_tracker.kp_src],
                                self.cur_data[self.cfg.e_tracker.kp_src],
                                not(self.cfg.e_tracker.iterative_kp.enable)) # pose: from cur->ref
                E_pose = e_tracker_outputs['pose']
                self.timers.end('E-tracker')

                # Rotation
                hybrid_pose.R = E_pose.R

                # save inliers
                self.ref_data['inliers'] = e_tracker_outputs['inliers']

                # scale recovery
                if np.linalg.norm(E_pose.t) != 0:
                    self.timers.start('scale_recovery', 'tracking')
                    scale_out = self.e_tracker.scale_recovery(self.cur_data, self.ref_data, E_pose, False)
                    scale = scale_out['scale']
                    if self.cfg.scale_recovery.kp_src == 'kp_depth':
                        self.cur_data['kp_depth'] = scale_out['cur_kp_depth']
                        self.ref_data['kp_depth'] = scale_out['ref_kp_depth']
                        self.cur_data['rigid_flow_mask'] = scale_out['rigid_flow_mask']
                    if scale != -1:
                        hybrid_pose.t = E_pose.t * scale
                    self.timers.end('scale_recovery')

                # Iterative keypoint refinement
                if np.linalg.norm(E_pose.t) != 0 and self.cfg.e_tracker.iterative_kp.enable:
                    self.timers.start('E-tracker iter.', 'tracking')
                    # Compute refined keypoint
                    self.e_tracker.compute_rigid_flow_kp(self.cur_data,
                                                         self.ref_data,
                                                         hybrid_pose)

                    e_tracker_outputs = self.e_tracker.compute_pose_2d2d(
                                self.ref_data[self.cfg.e_tracker.iterative_kp.kp_src],
                                self.cur_data[self.cfg.e_tracker.iterative_kp.kp_src],
                                True) # pose: from cur->ref
                    E_pose = e_tracker_outputs['pose']

                    # Rotation
                    hybrid_pose.R = E_pose.R

                    # save inliers
                    self.ref_data['inliers'] = e_tracker_outputs['inliers']

                    # scale recovery
                    if np.linalg.norm(E_pose.t) != 0 and self.cfg.scale_recovery.iterative_kp.enable:
                        scale_out = self.e_tracker.scale_recovery(self.cur_data, self.ref_data, E_pose, True)
                        scale = scale_out['scale']
                        if scale != -1:
                            hybrid_pose.t = E_pose.t * scale
                    else:
                        hybrid_pose.t = E_pose.t * scale

                    print(f" ** {hybrid_pose.t}")
                    self.timers.end('E-tracker iter.')

            ''' PnP-tracker '''
            if self.tracking_method in ['PnP', 'hybrid']:
                # PnP if Essential matrix fail
                if np.linalg.norm(E_pose.t) == 0 or scale == -1:
                    self.timers.start('pnp', 'tracking')
                    pnp_outputs = self.pnp_tracker.compute_pose_3d2d(
                                    self.ref_data[self.cfg.pnp_tracker.kp_src],
                                    self.cur_data[self.cfg.pnp_tracker.kp_src],
                                    self.ref_data['depth'],
                                    not(self.cfg.pnp_tracker.iterative_kp.enable)
                                    ) # pose: from cur->ref
                    
                    # Iterative keypoint refinement
                    if self.cfg.pnp_tracker.iterative_kp.enable:
                        self.pnp_tracker.compute_rigid_flow_kp(self.cur_data, self.ref_data, pnp_outputs['pose'])
                        pnp_outputs = self.pnp_tracker.compute_pose_3d2d(
                                    self.ref_data[self.cfg.pnp_tracker.iterative_kp.kp_src],
                                    self.cur_data[self.cfg.pnp_tracker.iterative_kp.kp_src],
                                    self.ref_data['depth'],
                                    True
                                    ) # pose: from cur->ref

                    self.timers.end('pnp')

                    # use PnP pose instead of E-pose
                    hybrid_pose = pnp_outputs['pose']
                    self.tracking_mode = "PnP"

            ''' Deep-tracker '''
            if self.tracking_method in ['deep_pose']:
                hybrid_pose = SE3(self.ref_data['deep_pose'])
                self.tracking_mode = "DeepPose"

            ''' Summarize data '''
            # update global poses
            self.ref_data['pose'] = copy.deepcopy(hybrid_pose)
            self.ref_data['motion'] = copy.deepcopy(hybrid_pose)
            pose = self.ref_data['pose']
            self.update_global_pose(pose, 1)

    def update_data(self, ref_data, cur_data):
        """Update data
        
        Args:
            ref_data (dict): reference data
            cur_data (dict): current data
        
        Returns:
            ref_data (dict): updated reference data
            cur_data (dict): updated current data
        """
        for key in cur_data:
            if key == "id":
                ref_data['id'] = cur_data['id']
            else:
                if ref_data.get(key, -1) is -1:
                    ref_data[key] = {}
                ref_data[key] = cur_data[key]
        
        # Delete unused flow to avoid data leakage
        ref_data['flow'] = None
        cur_data['flow'] = None
        ref_data['flow_diff'] = None
        return ref_data, cur_data

    def load_raw_data(self):
        """load image data and (optional) GT/precomputed depth data
        """
        # Reading image
        self.cur_data['img'] = self.dataset.get_image(self.cur_data['timestamp'])

        # Reading/Predicting depth
        if self.dataset.data_dir['depth_src'] is not None:
            self.cur_data['raw_depth'] = self.dataset.get_depth(self.cur_data['timestamp'])

    def main(self):
        """Main program
        """
        print("==> Start DF-VO")
        print("==> Running sequence: {}".format(self.cfg.seq))

        if self.cfg.no_confirm:
            start_frame = 0
        else:
            start_frame = int(input("Start with frame: "))

        for img_id in tqdm(range(start_frame, len(self.dataset), self.cfg.frame_step)):
            self.timers.start('DF-VO')
            self.tracking_mode = "Ess. Mat."

            """ Data reading """
            # Initialize ids and timestamps
            self.cur_data['id'] = img_id
            self.cur_data['timestamp'] = self.dataset.get_timestamp(img_id)

            # Read image data and (optional) precomputed depth data
            self.timers.start('data_loading')
            self.load_raw_data()
            self.timers.end('data_loading')



            """ Visual odometry """
            self.timers.start('tracking')
            self.tracking()
            self.timers.end('tracking')

            """ Online Finetuning 
            if self.tracking_stage >= 1 and self.cfg.online_finetune.enable:
                self.deep_models.finetune(self.ref_data['img'], self.cur_data['img'],
                                      self.ref_data['pose'].pose,
                                      self.dataset.cam_intrinsics.mat,
                                      self.dataset.cam_intrinsics.inv_mat)
            """
            """ Visualization 
            if self.cfg.visualization.enable:
                self.timers.start('visualization')
                self.drawer.main(self)
                self.timers.end('visualization')
            """
            """ Update reference and current data """
            self.ref_data, self.cur_data = self.update_data(
                                    self.ref_data,
                                    self.cur_data,
            )

            self.tracking_stage += 1

            self.timers.end('DF-VO')

        print("=> Finish!")



        """ Display & Save result """
        print("The result is saved in [{}].".format(self.cfg.directory.result_dir))
        # Save trajectory map
        print("Save VO map.")
        map_png = "{}/map.png".format(self.cfg.directory.result_dir)
        cv2.imwrite(map_png, self.drawer.data['traj'])

        # Save trajectory txt
        traj_txt = "{}/{}.txt".format(self.cfg.directory.result_dir, self.cfg.seq)
        self.dataset.save_result_traj(traj_txt, self.global_poses)

        # save finetuned model
        # if self.cfg.online_finetune.enable and self.cfg.online_finetune.save_model:
        #    self.deep_models.save_model()

        # Output experiement information
        self.timers.time_analysis()
