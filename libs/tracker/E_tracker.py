# Copyright (C) Huangying Zhan 2019. All rights reserved.
# This software is licensed under the terms in the LICENSE file 
# which allows for non-commercial use only.


import cv2
import copy
import multiprocessing as mp
import numpy as np
from sklearn import linear_model

from libs.camera_modules import SE3
from libs.geometry.ops_3d import *
from libs.utils import image_shape, image_grid
from libs.matching.kp_selection import good_depth_kp

def find_Ess_mat(inputs):
    # inputs
    kp_cur = inputs['kp_cur']
    kp_ref = inputs['kp_ref']
    H_inliers = inputs['H_inliers']
    cfg = inputs['cfg']
    cam_intrinsics = inputs['cam_intrinsics']

    # initialization
    valid_cfg = cfg.compute_2d2d_pose.validity
    principal_points = (cam_intrinsics.cx, cam_intrinsics.cy)
    fx = cam_intrinsics.fx

    # compute Ess
    E, inliers = cv2.findEssentialMat(
                        kp_cur,
                        kp_ref,
                        focal=fx,
                        pp=principal_points,
                        method=cv2.RANSAC,
                        prob=0.99,
                        threshold=cfg.compute_2d2d_pose.ransac.reproj_thre,
                        )
    # check homography inlier ratio
    if valid_cfg.method == "homo_ratio":
        H_inliers_ratio = H_inliers.sum()/(H_inliers.sum()+inliers.sum())
        valid_case = H_inliers_ratio < valid_cfg.thre
    elif valid_cfg.method == "flow":
        cheirality_cnt, R, t, _ = cv2.recoverPose(E, kp_cur, kp_ref,
                                focal=cam_intrinsics.fx,
                                pp=principal_points,)
        valid_case = cheirality_cnt > kp_cur.shape[0]*0.05
    
    # gather output
    outputs = {}
    outputs['E'] = E
    outputs['valid_case'] = valid_case
    outputs['inlier_cnt'] = inliers.sum()
    outputs['inlier'] = inliers

    return outputs


class EssTracker():
    def __init__(self, cfg, cam_intrinsics):
        self.cfg = cfg
        self.prev_scale = 1
        self.cam_intrinsics = cam_intrinsics

        if self.cfg.use_multiprocessing:
            self.p = mp.Pool(5)

    def compute_pose_2d2d(self, kp_ref, kp_cur):
        """Compute the pose from view2 to view1
        Args:
            kp_ref (Nx2 array): keypoints for reference view
            kp_cur (Nx2 array): keypoints for current view
            cam_intrinsics (Intrinsics)
        Returns:
            outputs (dict):
                - pose (SE3): relative pose from current to reference view
                - best_inliers (N boolean array): inlier mask
        """
        principal_points = (self.cam_intrinsics.cx, self.cam_intrinsics.cy)

        # validity check
        valid_cfg = self.cfg.compute_2d2d_pose.validity
        valid_case = True

        # initialize ransac setup
        R = np.eye(3)
        t = np.zeros((3,1))
        best_Rt = [R, t]
        best_inlier_cnt = 0
        max_ransac_iter = self.cfg.compute_2d2d_pose.ransac.repeat
        best_inliers = np.ones((kp_ref.shape[0], 1)) == 1

        if valid_cfg.method == "flow":
            # check flow magnitude
            avg_flow = np.mean(np.linalg.norm(kp_ref-kp_cur, axis=1))
            valid_case = avg_flow > valid_cfg.thre
        
        elif valid_cfg.method == "homo_ratio":
            # Find homography
            H, H_inliers = cv2.findHomography(
                        kp_cur,
                        kp_ref,
                        method=cv2.RANSAC,
                        confidence=0.99,
                        ransacReprojThreshold=0.2,
                        )
        
        if valid_case:
            num_valid_case = 0
            for i in range(max_ransac_iter): # repeat ransac for several times for stable result
                # shuffle kp_cur and kp_ref (only useful when random seed is fixed)	
                new_list = np.arange(0, kp_cur.shape[0], 1)	
                np.random.shuffle(new_list)
                new_kp_cur = kp_cur.copy()[new_list]
                new_kp_ref = kp_ref.copy()[new_list]

                E, inliers = cv2.findEssentialMat(
                            new_kp_cur,
                            new_kp_ref,
                            focal=self.cam_intrinsics.fx,
                            pp=principal_points,
                            method=cv2.RANSAC,
                            prob=0.99,
                            threshold=self.cfg.compute_2d2d_pose.ransac.reproj_thre,
                            )
                
                # check homography inlier ratio
                if valid_cfg.method == "homo_ratio":
                    H_inliers_ratio = H_inliers.sum()/(H_inliers.sum()+inliers.sum())
                    valid_case = H_inliers_ratio < valid_cfg.thre
                    # print("valid: {}; ratio: {}".format(valid_case, H_inliers_ratio))

                    # inlier check
                    inlier_check = inliers.sum() > best_inlier_cnt
                elif valid_cfg.method == "flow":
                    cheirality_cnt, R, t, _ = cv2.recoverPose(E, new_kp_cur, new_kp_ref,
                                            focal=self.cam_intrinsics.fx,
                                            pp=principal_points)
                    valid_case = cheirality_cnt > kp_cur.shape[0]*0.1
                    
                    # inlier check
                    inlier_check = inliers.sum() > best_inlier_cnt and cheirality_cnt > kp_cur.shape[0]*0.05

                # save best_E
                if inlier_check:
                    best_E = E
                    best_inlier_cnt = inliers.sum()

                    revert_new_list = np.zeros_like(new_list)
                    for cnt, i in enumerate(new_list):
                        revert_new_list[i] = cnt
                    best_inliers = inliers[list(revert_new_list)]
                num_valid_case += (valid_case * 1)

            major_valid = num_valid_case > (max_ransac_iter/2)
            if major_valid:
                cheirality_cnt, R, t, _ = cv2.recoverPose(best_E, kp_cur, kp_ref,
                                        focal=self.cam_intrinsics.fx,
                                        pp=principal_points,
                                        )

                # cheirality_check
                if cheirality_cnt > kp_cur.shape[0]*0.1:
                    best_Rt = [R, t]

        R, t = best_Rt
        pose = SE3()
        pose.R = R
        pose.t = t
        outputs = {"pose": pose, "inliers": best_inliers[:,0]==1}
        return outputs

    def compute_pose_2d2d_mp(self, kp_ref, kp_cur):
        """Compute the pose from view2 to view1 (multiprocessing ver)
        Args:
            kp_ref (Nx2 array): keypoints for reference view
            kp_cur (Nx2 array): keypoints for current view
        Returns:
            outputs (dict):
                - pose (SE3): relative pose from current to reference view
                - best_inliers (N boolean array): inlier mask
        """
        start_time = time()
        principal_points = (self.cam_intrinsics.cx, self.cam_intrinsics.cy)

        # validity check
        valid_cfg = self.cfg.compute_2d2d_pose.validity
        valid_case = True

        # initialize ransac setup
        R = np.eye(3)
        t = np.zeros((3,1))
        best_Rt = [R, t]
        max_ransac_iter = self.cfg.compute_2d2d_pose.ransac.repeat

        if valid_cfg.method == "flow":
            # check flow magnitude
            avg_flow = np.mean(np.linalg.norm(kp_ref-kp_cur, axis=1))
            valid_case = avg_flow > valid_cfg.thre
        
        elif valid_cfg.method == "homo_ratio":
            # Find homography
            H, H_inliers = cv2.findHomography(
                        kp_cur,
                        kp_ref,
                        method=cv2.RANSAC,
                        confidence=0.99,
                        ransacReprojThreshold=0.2,
                        )
        
        if valid_case:
            inputs_mp = []
            outputs_mp = []
            for i in range(max_ransac_iter):
                # shuffle kp_cur and kp_ref
                new_list = np.arange(0, kp_cur.shape[0], 1)
                np.random.shuffle(new_list)
                new_kp_cur = kp_cur.copy()[new_list]
                new_kp_ref = kp_ref.copy()[new_list]

                inputs = {}
                inputs['kp_cur'] = new_kp_cur
                inputs['kp_ref'] = new_kp_ref
                inputs['H_inliers'] = H_inliers
                inputs['cfg'] = self.cfg
                inputs['cam_intrinsics'] = self.cam_intrinsics
                inputs_mp.append(inputs)
            outputs_mp = self.p.map(find_Ess_mat, inputs_mp)

            # Gather result
            num_valid_case = 0
            best_inlier_cnt = 0
            best_inliers = np.ones((kp_ref.shape[0])) == 1
            for outputs in outputs_mp:
                num_valid_case += outputs['valid_case']
                if outputs['inlier_cnt'] > best_inlier_cnt:
                    best_E = outputs['E']
                    best_inlier_cnt = outputs['inlier_cnt']
                    best_inliers = outputs['inlier']

            # Recover pose
            major_valid = num_valid_case > (max_ransac_iter/2)
            if major_valid:
                cheirality_cnt, R, t, _ = cv2.recoverPose(best_E, new_kp_cur, new_kp_ref,
                                        focal=self.cam_intrinsics.fx,
                                        pp=principal_points,)

                # cheirality_check
                if cheirality_cnt > kp_cur.shape[0]*0.1:
                    best_Rt = [R, t]

        R, t = best_Rt
        pose = SE3()
        pose.R = R
        pose.t = t
        self.timers.count('Ess. Mat.', time()-start_time)

        outputs = {"pose": pose, "inliers": best_inliers}
        return outputs

    def scale_recovery(self, cur_data, ref_data, E_pose, ref_id):
        """recover depth scale
        """

        if self.cfg.translation_scale.method == "single":
            scale = self.scale_recovery_single(cur_data, ref_data, E_pose, ref_id)
        
        elif self.cfg.translation_scale.method == "iterative":
            scale = self.scale_recovery_iterative(cur_data, ref_data, E_pose, ref_id)
        return scale

    def scale_recovery_single(self, cur_data, ref_data, E_pose, ref_id):
        """recover depth scale by comparing triangulated depths and CNN depths
        
        Args:
            cur_data (dict)
            ref_data (dict)
            E_pose (SE3)
        Returns:
            scale (float)
        """
        if self.cfg.translation_scale.kp_src == "kp_best":
            ref_kp = cur_data[self.cfg.translation_scale.kp_src]
            cur_kp = ref_data[self.cfg.translation_scale.kp_src][ref_id]
        elif self.cfg.translation_scale.kp_src == "kp_depth":
            ref_kp = cur_data[self.cfg.translation_scale.kp_src]
            cur_kp = ref_data[self.cfg.translation_scale.kp_src][ref_id]

        scale = self.find_scale_from_depth(
            ref_kp,
            cur_kp,
            E_pose.inv_pose, 
            cur_data['depth']
        )
        return scale

    def scale_recovery_iterative(self, cur_data, ref_data, E_pose, ref_id):
        """recover depth scale by comparing triangulated depths and CNN depths
        Iterative scale recovery is applied
        
        Args:
            cur_data (dict)
            ref_data (dict)
            E_pose (SE3)
        Returns:
            scale (float)
        """
        # Initialization
        scale = self.prev_scale
        delta = 0.001
        ref_data['rigid_flow_pose'] = {}

        for _ in range(5):    
            rigid_flow_pose = copy.deepcopy(E_pose)
            rigid_flow_pose.t *= scale

            ref_data['rigid_flow_pose'][ref_id] = SE3(rigid_flow_pose.inv_pose)

            # kp selection
            kp_sel_outputs = self.kp_selection_good_depth(cur_data, ref_data)
            ref_data['kp_depth'] = {}
            cur_data['kp_depth'] = kp_sel_outputs['kp1_depth'][0]
            for ref_id in ref_data['id']:
                ref_data['kp_depth'][ref_id] = kp_sel_outputs['kp2_depth'][ref_id][0]
            
            cur_data['rigid_flow_mask'] = kp_sel_outputs['rigid_flow_mask']
            
            # translation scale from triangulation v.s. CNN-depth
            if self.cfg.translation_scale.kp_src == "kp_best":
                ref_kp = cur_data[self.cfg.translation_scale.kp_src][ref_data['inliers'][ref_id]]
                cur_kp = ref_data[self.cfg.translation_scale.kp_src][ref_id][ref_data['inliers'][ref_id]]
            elif self.cfg.translation_scale.kp_src == "kp_depth":
                ref_kp = cur_data[self.cfg.translation_scale.kp_src]
                cur_kp = ref_data[self.cfg.translation_scale.kp_src][ref_id]
            new_scale = self.find_scale_from_depth(
                ref_kp,
                cur_kp,
                E_pose.inv_pose, 
                cur_data['depth']
            )

            delta_scale = np.abs(new_scale-scale)
            scale = new_scale
            self.prev_scale = new_scale
            
            if delta_scale < delta:
                return scale
        return scale

    def find_scale_from_depth(self, kp1, kp2, T_21, depth2):
        """Compute VO scaling factor for T_21
        Args:
            kp1 (Nx2 array): reference kp
            kp2 (Nx2 array): current kp
            T_21 (4x4 array): relative pose; from view 1 to view 2
            depth2 (HxW array): depth 2
        Returns:
            scale (float): scaling factor
        """
        # Triangulation
        img_h, img_w, _ = image_shape(depth2)
        kp1_norm = kp1.copy()
        kp2_norm = kp2.copy()

        kp1_norm[:, 0] = \
            (kp1[:, 0] - self.cam_intrinsics.cx) / self.cam_intrinsics.fx
        kp1_norm[:, 1] = \
            (kp1[:, 1] - self.cam_intrinsics.cy) / self.cam_intrinsics.fy
        kp2_norm[:, 0] = \
            (kp2[:, 0] - self.cam_intrinsics.cx) / self.cam_intrinsics.fx
        kp2_norm[:, 1] = \
            (kp2[:, 1] - self.cam_intrinsics.cy) / self.cam_intrinsics.fy

        _, X1_tri, X2_tri = triangulation(kp1_norm, kp2_norm, np.eye(4), T_21)

        # Triangulation outlier removal
        depth2_tri = convert_sparse3D_to_depth(kp2, X2_tri, img_h, img_w)
        depth2_tri[depth2_tri < 0] = 0

        # common mask filtering
        non_zero_mask_pred2 = (depth2 > 0)
        non_zero_mask_tri2 = (depth2_tri > 0)
        valid_mask2 = non_zero_mask_pred2 * non_zero_mask_tri2

        # if self.cfg.debug and False:
        #     # print("max: ", depth2_tri.max())
        #     # print("median: ", np.median(depth2_tri))
        #     f = plt.figure("depth2_tri")
        #     plt.imshow(depth2_tri, vmin=0, vmax=200)
        #     f,ax = plt.subplots(3,1, num="mask;depth2_tri, depth2")
        #     ax[0].imshow(valid_mask2*1.)
        #     ax[1].imshow(depth2_tri)
        #     ax[2].imshow(depth2)
        #     plt.show()

        # depth_pred_non_zero = np.concatenate([depth2[valid_mask2], depth1[valid_mask1]])
        # depth_tri_non_zero = np.concatenate([depth2_tri[valid_mask2], depth1_tri[valid_mask1]])
        depth_pred_non_zero = np.concatenate([depth2[valid_mask2]])
        depth_tri_non_zero = np.concatenate([depth2_tri[valid_mask2]])
        depth_ratio = depth_tri_non_zero / depth_pred_non_zero
        
        # Estimate scale (ransac)
        # if (valid_mask1.sum() + valid_mask2.sum()) > 10:
        if valid_mask2.sum() > 10:
            # RANSAC scaling solver
            ransac = linear_model.RANSACRegressor(
                        base_estimator=linear_model.LinearRegression(
                            fit_intercept=False),
                        min_samples=self.cfg.translation_scale.ransac.min_samples,
                        max_trials=self.cfg.translation_scale.ransac.max_trials,
                        stop_probability=self.cfg.translation_scale.ransac.stop_prob,
                        residual_threshold=self.cfg.translation_scale.ransac.thre
                        )
            if self.cfg.translation_scale.ransac.method == "depth_ratio":
                ransac.fit(
                    depth_ratio.reshape(-1, 1),
                    np.ones((depth_ratio.shape[0],1))
                    )
            elif self.cfg.translation_scale.ransac.method == "abs_diff":
                ransac.fit(
                    depth_tri_non_zero.reshape(-1, 1),
                    depth_pred_non_zero.reshape(-1, 1),
                )
            scale = ransac.estimator_.coef_[0, 0]

            # # scale outlier
            # if ransac.inlier_mask_.sum() / depth_ratio.shape[0] < 0.2:
            #     scale = -1
        else:
            scale = -1

        return scale

    def kp_selection_good_depth(self, cur_data, ref_data):
        """Choose valid kp from a series of operations
        """
        outputs = {}

        # initialization
        h, w = cur_data['depth'].shape
        ref_id = ref_data['id'][0]

        kp1 = image_grid(h, w)
        kp1 = np.expand_dims(kp1, 0)
        tmp_flow_data = np.transpose(np.expand_dims(ref_data['flow'][ref_id], 0), (0, 2, 3, 1))
        kp2 = kp1 + tmp_flow_data

        """ depth consistent kp selection """
        if self.cfg.kp_selection.good_depth_kp.enable:
            ref_data['rigid_flow_diff'] = {}
            # compute rigid flow
            rigid_flow_pose = ref_data['rigid_flow_pose'][ref_id].pose
            rigid_flow = self.compute_rigid_flow(
                                        ref_data['raw_depth'][ref_id],  
                                        # cur_data['raw_depth'],
                                        # np.linalg.inv(ref_data['deep_pose'][ref_id])
                                        rigid_flow_pose
                                        )
            rigid_flow_diff = np.linalg.norm(
                                rigid_flow - ref_data['flow'][ref_id].transpose(1,2,0),
                                axis=2)
            rigid_flow_diff = np.expand_dims(rigid_flow_diff, 2)
            
            # print("pose: ", np.linalg.inv(ref_data['deep_pose'][ref_id]))
            # print("gt_pose: ", gt_pose)
            # plt.figure("depth")
            # plt.imshow(self.ref_data['raw_depth'][ref_id])
            # plt.figure("rigid_flow x")
            # plt.imshow(rigid_flow[:,:,0])
            # plt.figure("rigid_flow y")
            # plt.imshow(rigid_flow[:,:,1])
            # plt.figure("ref_flow")
            # plt.imshow(ref_data['flow'][ref_id][0, :,:])
            # plt.figure("flow_diff")
            # plt.imshow(rigid_flow_diff, vmin=0, vmax=3)
            # plt.show()

            ref_data['rigid_flow_diff'][ref_id] = rigid_flow_diff

            # get depth-flow consistent kp
            outputs.update(
                good_depth_kp(
                    kp1=kp1,
                    kp2=kp2,
                    ref_data=ref_data,
                    cfg=self.cfg,
                    outputs=outputs
                    )
            )

        return outputs

    def unprojection(self, depth, cam_intrinsics):
        """Convert a depth map to XYZ
        Args:
            depth (HxW array): depth map
            cam_intrinsics (Intrinsics): camera intrinsics
        Returns:
            XYZ (HxWx3): 3D coordinates
        """
        height, width = depth.shape

        depth_mask = (depth != 0)
        depth_mask = np.repeat(np.expand_dims(depth_mask, 2), 3, axis=2)

        # initialize regular grid
        XYZ = np.ones((height, width, 3, 1))
        XYZ[:, :, 0, 0] = np.repeat(
                            np.expand_dims(np.arange(0, width), 0),
                            height, axis=0
                            )
        XYZ[:, :, 1, 0] = np.repeat(
                            np.expand_dims(np.arange(0, height), 1),
                            width, axis=1)

        inv_K = np.ones((1, 1, 3, 3))
        inv_K[0, 0] = cam_intrinsics.inv_mat
        inv_K = np.repeat(inv_K, height, axis=0)
        inv_K = np.repeat(inv_K, width, axis=1)

        XYZ = np.matmul(inv_K, XYZ)[:, :, :, 0]
        XYZ[:, :, 0] = XYZ[:, :, 0] * depth
        XYZ[:, :, 1] = XYZ[:, :, 1] * depth
        XYZ[:, :, 2] = XYZ[:, :, 2] * depth
        return XYZ

    def projection(self, XYZ, proj_mat):
        """Convert XYZ to [u,v,d]
        Args:
            XYZ (HxWx3): 3D coordinates
            proj_mat (3x3 array): camera intrinsics / projection matrix
        Returns:
            xy (HxWx2 array): projected image coordinates
        """
        if len(XYZ.shape) == 3:
            h, w, _ = XYZ.shape
            tmp_XYZ = XYZ.copy()
            tmp_XYZ[:, :, 0] /= tmp_XYZ[:, :, 2]
            tmp_XYZ[:, :, 1] /= tmp_XYZ[:, :, 2]
            tmp_XYZ[:, :, 2] /= tmp_XYZ[:, :, 2]
            tmp_XYZ = np.expand_dims(tmp_XYZ, axis=3)
            K = np.ones((1, 1, 3, 3))
            K[0, 0] = proj_mat
            K = np.repeat(K, h, axis=0)
            K = np.repeat(K, w, axis=1)

            xy = np.matmul(K, tmp_XYZ)[:, :, :2, 0]
        elif len(XYZ.shape) == 2:
            n, _ = XYZ.shape
            tmp_XYZ = XYZ.copy()
            tmp_XYZ[:, 0] /= tmp_XYZ[:, 2]
            tmp_XYZ[:, 1] /= tmp_XYZ[:, 2]
            tmp_XYZ[:, 2] /= tmp_XYZ[:, 2]
            tmp_XYZ = np.expand_dims(tmp_XYZ, axis=2)
            K = np.ones((1, 3, 3))
            K[0] = proj_mat
            K = np.repeat(K, n, axis=0)

            xy = np.matmul(K, tmp_XYZ)[:, :2, 0]
        return xy

    def transform_XYZ(self, XYZ, transformation):
        """Transform point cloud
        Args:
            XYZ (HxWx3 / Nx3): 3D coordinates
            pose (4x4 array): tranformation matrix
        Returns:
            new_XYZ (HxWx3 / Nx3): 3D coordinates
        """
        if len(XYZ.shape) == 3:
            h, w, _ = XYZ.shape
            new_XYZ = np.ones((h, w, 4, 1))
            new_XYZ[:, :, :3, 0] = XYZ

            T = np.ones((1, 1, 4, 4))
            T[0, 0] = transformation
            T = np.repeat(T, h, axis=0)
            T = np.repeat(T, w, axis=1)

            new_XYZ = np.matmul(T, new_XYZ)[:, :, :3, 0]
        elif len(XYZ.shape) == 2:
            n, _ = XYZ.shape
            new_XYZ = np.ones((n, 4, 1))
            new_XYZ[:, :3, 0] = XYZ

            T = np.ones((1, 4, 4))
            T[0] = transformation
            T = np.repeat(T, n, axis=0)

            new_XYZ = np.matmul(T, new_XYZ)[:, :3, 0]

        return new_XYZ

    def xy_to_uv(self, xy):
        """Convert image coordinates to optical flow
        Args:
            xy (HxWx2 array): image coordinates
        Returns:
            uv (HxWx2 array): x-flow and y-flow
        """
        h, w, _ = xy.shape
        img_grid = image_grid(h, w)
        uv = xy - img_grid
        return uv

    def compute_rigid_flow(self, depth, pose):
        """Compute rigid flow 
        Args:
            depth (HxW array): depth map of reference view
            pose (4x4 array): from reference to current
        Returns:
            rigid_flow (HxWx2): rigid flow [x-flow, y-flow]
        """
        XYZ_ref = self.unprojection(depth, self.cam_intrinsics)
        XYZ_cur = self.transform_XYZ(XYZ_ref, pose)
        xy = self.projection(XYZ_cur, self.cam_intrinsics.mat)
        rigid_flow = self.xy_to_uv(xy)
        return rigid_flow