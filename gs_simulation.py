import sys

sys.path.append("gaussian-splatting")


import argparse
import math
import cv2
import torch
import torch.nn.functional as F
import os
import numpy as np
import json
from tqdm import tqdm
from typing import Dict, List, Tuple


# Gaussian splatting dependencies
from utils.sh_utils import eval_sh

from scene.gaussian_model_3dgsdr import GaussianModel
# import diff_gaussian_rasterization_c7 

# from diff_gaussian_rasterization import (
#     GaussianRasterizationSettings,
#     GaussianRasterizer,
# )

from scene.cameras import Camera as GSCamera
# from gaussian_renderer import render, GaussianModel
from utils.system_utils import searchForMaxIteration
from utils.graphics_utils import focal2fov
from utils.general_utils import sample_camera_rays, get_env_rayd1, get_env_rayd2

# MPM dependencies
from mpm_solver_warp.engine_utils import *
from mpm_solver_warp.mpm_solver_warp import MPM_Simulator_WARP
import warp as wp

# Particle filling dependencies
from particle_filling.filling import *

# Utils
from utils.decode_param import *
from utils.transformation_utils import *
from utils.camera_view_utils import *
from utils.render_utils import *


import nvdiffrast.torch as dr
def read_hdr(path: str) -> np.ndarray:
    """Reads an HDR map from disk.

    Args:
        path (str): Path to the .hdr file.

    Returns:
        numpy.ndarray: Loaded (float) HDR map with RGB channels in order.
    """
    with open(path, "rb") as h:
        buffer_ = np.frombuffer(h.read(), np.uint8)
    bgr = cv2.imdecode(buffer_, cv2.IMREAD_UNCHANGED)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return rgb

def cube_to_dir(s: int, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    if s == 0:
        rx, ry, rz = torch.ones_like(x), -y, -x
    elif s == 1:
        rx, ry, rz = -torch.ones_like(x), -y, x
    elif s == 2:
        rx, ry, rz = x, torch.ones_like(x), y
    elif s == 3:
        rx, ry, rz = x, -torch.ones_like(x), -y
    elif s == 4:
        rx, ry, rz = x, -y, torch.ones_like(x)
    elif s == 5:
        rx, ry, rz = -x, -y, -torch.ones_like(x)
    return torch.stack((rx, ry, rz), dim=-1)

def latlong_to_cubemap(latlong_map: torch.Tensor, res: List[int]) -> torch.Tensor:
    cubemap = torch.zeros(
        6, res[0], res[1], latlong_map.shape[-1], dtype=torch.float32, device="cuda"
    )
    for s in range(6):
        gy, gx = torch.meshgrid(
            torch.linspace(-1.0 + 1.0 / res[0], 1.0 - 1.0 / res[0], res[0], device="cuda"),
            torch.linspace(-1.0 + 1.0 / res[1], 1.0 - 1.0 / res[1], res[1], device="cuda"),
            indexing="ij",
        )
        v = F.normalize(cube_to_dir(s, gx, gy), p=2, dim=-1)

        tu = torch.atan2(v[..., 0:1], -v[..., 2:3]) / (2 * np.pi) + 0.5
        tv = torch.acos(torch.clamp(v[..., 1:2], min=-1, max=1)) / np.pi
        texcoord = torch.cat((tu, tv), dim=-1)

        cubemap[s, ...] = dr.texture(
            latlong_map[None, ...], texcoord[None, ...], filter_mode="linear"
        )[0]
    return cubemap.permute(0, 3, 1, 2)


wp.init()
wp.config.verify_cuda = True

ti.init(arch=ti.cuda, device_memory_GB=8.0)


class PipelineParamsNoparse:
    """Same as PipelineParams but without argument parser."""

    def __init__(self):
        self.convert_SHs_python = False
        self.compute_cov3D_python = False
        self.debug = False


def load_checkpoint(model_path, sh_degree=3, iteration=-1):
    # Find checkpoint
    checkpt_dir = os.path.join(model_path, "point_cloud")
    if iteration == -1:
        iteration = searchForMaxIteration(checkpt_dir)
    print(f"Loading checkpoint from iteration {iteration}")
    checkpt_path = os.path.join(
        checkpt_dir, f"iteration_{iteration}", "point_cloud.ply"
    )

    # Load guassians
    gaussians = GaussianModel(sh_degree)
    gaussians.load_ply(checkpt_path)
    return gaussians

def get_normals_from_cov(pts,cov3d_tensor,cam_o):
    p2o = cam_o[None] - pts
    
    cov3d_matrix = cov3d_tensor_to_matrix(cov3d_tensor)
    rot_matrix, scales, _ = torch.svd(cov3d_matrix)

    # print("rot_matrix 0", rot_matrix[1250])
    # print("rot_matrix 0", rot_t[1250])
    # print("rot_matrix det", torch.linalg.det(rot_matrix[12]))
    # print("r*rt:", torch.matmul(rot_matrix[12], rot_matrix[12].T))
    # assert torch.allclose(rot_matrix, rot_t, rtol=1e-2, atol=1e-2)

    min_axis_id = torch.argmin(scales, dim = -1, keepdim=True)
    min_axis = torch.zeros_like(scales).scatter(1, min_axis_id, 1)
    
    ndir = torch.bmm(rot_matrix, min_axis.unsqueeze(-1)).squeeze(-1)
    neg_msk = torch.sum(p2o*ndir, dim=-1) < 0
    ndir[neg_msk] = -ndir[neg_msk] # make sure normal orient to camera
    return ndir


def cov3d_tensor_to_matrix(cov3d_tensor):
    cov3d_tensor = cov3d_tensor.view(-1, 6)
    cov3d_matrix = torch.zeros((cov3d_tensor.shape[0], 3, 3), device=cov3d_tensor.device)
    cov3d_matrix[:, 0, 0] = cov3d_tensor[:, 0]
    cov3d_matrix[:, 0, 1] = cov3d_tensor[:, 1]
    cov3d_matrix[:, 0, 2] = cov3d_tensor[:, 2]
    cov3d_matrix[:, 1, 0] = cov3d_tensor[:, 1]
    cov3d_matrix[:, 1, 1] = cov3d_tensor[:, 3]
    cov3d_matrix[:, 1, 2] = cov3d_tensor[:, 4]
    cov3d_matrix[:, 2, 0] = cov3d_tensor[:, 2]
    cov3d_matrix[:, 2, 1] = cov3d_tensor[:, 4]
    cov3d_matrix[:, 2, 2] = cov3d_tensor[:, 5]
    return cov3d_matrix

# rayd: x,3, from camera to world points
# normal: x,3
# all normalized
def reflection(rayd, normal):
    refl = rayd - 2*normal*torch.sum(rayd*normal, dim=-1, keepdim=True)
    return refl

def sample_cubemap_color(rays_d, env_map):
    H,W = rays_d.shape[:2]
    outcolor = torch.sigmoid(env_map(rays_d.reshape(-1,3)))
    outcolor = outcolor.reshape(H,W,3).permute(2,0,1)
    return outcolor

def get_refl_color(envmap: torch.Tensor, HWK, R, T, normal_map): #RT W2C
    rays_d = sample_camera_rays(HWK, R, T)
    rays_d = reflection(rays_d, normal_map)
    #rays_d = rays_d.clamp(-1, 1) # avoid numerical error when arccos
    return sample_cubemap_color(rays_d, envmap)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path", type=str, required=True)
    parser.add_argument("--output_path", type=str, default=None)
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--output_ply", action="store_true")
    parser.add_argument("--output_h5", action="store_true")
    parser.add_argument("--render_img", action="store_true")
    parser.add_argument("--compile_video", action="store_true")
    parser.add_argument("--white_bg", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--relight", action="store_true")
    parser.add_argument("--hdri_path", type=str, default=None)
    args = parser.parse_args()



    if not os.path.exists(args.model_path):
        AssertionError("Model path does not exist!")
    if not os.path.exists(args.config):
        AssertionError("Scene config does not exist!")
    if args.output_path is not None and not os.path.exists(args.output_path):
        os.makedirs(args.output_path)

    normap_path = os.path.join(args.output_path, "normals")
    refl_path = os.path.join(args.output_path, "refl")
    if not os.path.exists(normap_path):
        os.makedirs(normap_path)
    if not os.path.exists(refl_path):
        os.makedirs(refl_path)
    base_color_path = os.path.join(args.output_path, "base_color")
    if not os.path.exists(base_color_path):
        os.makedirs(base_color_path)
    final_image_path = os.path.join(args.output_path, "final_image")
    if not os.path.exists(final_image_path):
        os.makedirs(final_image_path)  
    

    # load scene config
    print("Loading scene config...")
    (
        material_params,
        bc_params,
        time_params,
        preprocessing_params,
        camera_params,
    ) = decode_param_json(args.config)

    # load gaussians
    print("Loading gaussians...")
    model_path = args.model_path
    gaussians = load_checkpoint(model_path)
    pipeline = PipelineParamsNoparse()
    pipeline.compute_cov3D_python = True
    background = (
        torch.tensor([1, 1, 1], dtype=torch.float32, device="cuda")
        if args.white_bg
        else torch.tensor([0, 0, 0], dtype=torch.float32, device="cuda")
    )

    # init the scene
    print("Initializing scene and pre-processing...")
    params = load_params_from_gs(gaussians, pipeline)


    # envmap = gaussians.get_envmap
    # print(repr(envmap))
    if args.hdri_path is not None and args.relight and not args.debug and args.render_img:
        with torch.no_grad():
            hdri_path = args.hdri_path
            print(f"read hdri from {hdri_path}")
            hdri = read_hdr(hdri_path)
            hdri = torch.from_numpy(hdri).cuda()
            res = 128
            envmap = gaussians.get_envmap
            envmap.params["Cubemap_texture"] = latlong_to_cubemap(hdri, [res, res])
            gaussians.env_map = envmap.cuda()

    init_pos = params["pos"]
    init_cov = params["cov3D_precomp"]
    init_screen_points = params["screen_points"]
    init_opacity = params["opacity"]
    init_shs = params["shs"]
    init_refl = params["refl"]

    # throw away low opacity kernels
    mask = init_opacity[:, 0] > preprocessing_params["opacity_threshold"]
    init_pos = init_pos[mask, :]
    init_cov = init_cov[mask, :]
    init_opacity = init_opacity[mask, :]
    init_screen_points = init_screen_points[mask, :]
    init_shs = init_shs[mask, :]
    init_refl = init_refl[mask, :]

    # rorate and translate object
    if args.debug:
        if not os.path.exists("./log"):
            os.makedirs("./log")
        particle_position_tensor_to_ply(
            init_pos,
            "./log/init_particles.ply",
        )
    rotation_matrices = generate_rotation_matrices(
        torch.tensor(preprocessing_params["rotation_degree"]),
        preprocessing_params["rotation_axis"],
    )
    rotated_pos = apply_rotations(init_pos, rotation_matrices)

    if args.debug:
        particle_position_tensor_to_ply(rotated_pos, "./log/rotated_particles.ply")

    # select a sim area and save params of unslected particles
    unselected_pos, unselected_cov, unselected_opacity, unselected_shs = (
        None,
        None,
        None,
        None,
    )
    if preprocessing_params["sim_area"] is not None:
        boundary = preprocessing_params["sim_area"]
        assert len(boundary) == 6
        mask = torch.ones(rotated_pos.shape[0], dtype=torch.bool).to(device="cuda")
        for i in range(3):
            mask = torch.logical_and(mask, rotated_pos[:, i] > boundary[2 * i])
            mask = torch.logical_and(mask, rotated_pos[:, i] < boundary[2 * i + 1])

        unselected_pos = init_pos[~mask, :]
        unselected_cov = init_cov[~mask, :]
        unselected_opacity = init_opacity[~mask, :]
        unselected_shs = init_shs[~mask, :]

        # normals = 

        rotated_pos = rotated_pos[mask, :]
        init_cov = init_cov[mask, :]
        init_opacity = init_opacity[mask, :]
        init_shs = init_shs[mask, :]

    transformed_pos, scale_origin, original_mean_pos = transform2origin(rotated_pos)
    transformed_pos = shift2center111(transformed_pos)

    # modify covariance matrix accordingly
    init_cov = apply_cov_rotations(init_cov, rotation_matrices)
    init_cov = scale_origin * scale_origin * init_cov

    if args.debug:
        particle_position_tensor_to_ply(
            transformed_pos,
            "./log/transformed_particles.ply",
        )

    # fill particles if needed
    gs_num = transformed_pos.shape[0]
    device = "cuda:0"
    filling_params = preprocessing_params["particle_filling"]

    if filling_params is not None:
        print("Filling internal particles...")
        mpm_init_pos = fill_particles(
            pos=transformed_pos,
            opacity=init_opacity,
            cov=init_cov,
            grid_n=filling_params["n_grid"],
            max_samples=filling_params["max_particles_num"],
            grid_dx=material_params["grid_lim"] / filling_params["n_grid"],
            density_thres=filling_params["density_threshold"],
            search_thres=filling_params["search_threshold"],
            max_particles_per_cell=filling_params["max_partciels_per_cell"],
            search_exclude_dir=filling_params["search_exclude_direction"],
            ray_cast_dir=filling_params["ray_cast_direction"],
            boundary=filling_params["boundary"],
            smooth=filling_params["smooth"],
        ).to(device=device)

        if args.debug:
            particle_position_tensor_to_ply(mpm_init_pos, "./log/filled_particles.ply")
    else:
        mpm_init_pos = transformed_pos.to(device=device)

    # init the mpm solver
    print("Initializing MPM solver and setting up boundary conditions...")
    mpm_init_vol = get_particle_volume(
        mpm_init_pos,
        material_params["n_grid"],
        material_params["grid_lim"] / material_params["n_grid"],
        unifrom=material_params["material"] == "sand",
    ).to(device=device)

    if filling_params is not None and filling_params["visualize"] == True:
        shs, opacity, mpm_init_cov = init_filled_particles(
            mpm_init_pos[:gs_num], # initial gs particles
            init_shs,
            init_cov,
            init_opacity,
            mpm_init_pos[gs_num:], # filled particles
        )
        gs_num = mpm_init_pos.shape[0]
    else:
        mpm_init_cov = torch.zeros((mpm_init_pos.shape[0], 6), device=device)
        mpm_init_cov[:gs_num] = init_cov
        shs = init_shs
        opacity = init_opacity

    if args.debug:
        print("check *.ply files to see if it's ready for simulation")

    # set up the mpm solver
    mpm_solver = MPM_Simulator_WARP(10)
    mpm_solver.load_initial_data_from_torch(
        mpm_init_pos,
        mpm_init_vol,
        mpm_init_cov,
        n_grid=material_params["n_grid"],
        grid_lim=material_params["grid_lim"],
    )
    mpm_solver.set_parameters_dict(material_params)

    # Note: boundary conditions may depend on mass, so the order cannot be changed!
    set_boundary_conditions(mpm_solver, bc_params, time_params)

    mpm_solver.finalize_mu_lam()

    # camera setting
    mpm_space_viewpoint_center = (
        torch.tensor(camera_params["mpm_space_viewpoint_center"]).reshape((1, 3)).cuda()
    )
    mpm_space_vertical_upward_axis = (
        torch.tensor(camera_params["mpm_space_vertical_upward_axis"])
        .reshape((1, 3))
        .cuda()
    )
    (
        viewpoint_center_worldspace,
        observant_coordinates,
    ) = get_center_view_worldspace_and_observant_coordinate(
        mpm_space_viewpoint_center,
        mpm_space_vertical_upward_axis,
        rotation_matrices,
        scale_origin,
        original_mean_pos,
    )

    # run the simulation
    if args.output_ply or args.output_h5:
        directory_to_save = os.path.join(args.output_path, "simulation_ply")
        if not os.path.exists(directory_to_save):
            os.makedirs(directory_to_save)

        save_data_at_frame(
            mpm_solver,
            directory_to_save,
            0,
            save_to_ply=args.output_ply,
            save_to_h5=args.output_h5,
        )

    substep_dt = time_params["substep_dt"]
    frame_dt = time_params["frame_dt"]
    frame_num = time_params["frame_num"]
    step_per_frame = int(frame_dt / substep_dt)
    opacity_render = opacity
    shs_render = shs
    height = None
    width = None
    for frame in tqdm(range(frame_num)):
        current_camera = get_camera_view(
            model_path,
            default_camera_index=camera_params["default_camera_index"],
            center_view_world_space=viewpoint_center_worldspace,
            observant_coordinates=observant_coordinates,
            show_hint=camera_params["show_hint"],
            init_azimuthm=camera_params["init_azimuthm"],
            init_elevation=camera_params["init_elevation"],
            init_radius=camera_params["init_radius"],
            move_camera=camera_params["move_camera"],
            current_frame=frame,
            delta_a=camera_params["delta_a"],
            delta_e=camera_params["delta_e"],
            delta_r=camera_params["delta_r"],
        )

        # rasterize = initialize_resterize(
        #     current_camera, gaussians, pipeline, background
        # )
        rasterize = initialize_resterizer_3dgsdr(
            current_camera, gaussians, pipeline, background
        )

        for step in range(step_per_frame):
            mpm_solver.p2g2p(frame, substep_dt, device=device)

        if args.output_ply or args.output_h5:
            save_data_at_frame(
                mpm_solver,
                directory_to_save,
                frame + 1,
                save_to_ply=args.output_ply,
                save_to_h5=args.output_h5,
            )

        if args.render_img:
            pos = mpm_solver.export_particle_x_to_torch()[:gs_num].to(device)
            cov3D = mpm_solver.export_particle_cov_to_torch()
            rot = mpm_solver.export_particle_R_to_torch()
            cov3D = cov3D.view(-1, 6)[:gs_num].to(device)
            rot = rot.view(-1, 3, 3)[:gs_num].to(device)
            pos = apply_inverse_rotations(
                undotransform2origin(
                    undoshift2center111(pos), scale_origin, original_mean_pos
                ),
                rotation_matrices,
            )
            cov3D = cov3D / (scale_origin * scale_origin)
            cov3D = apply_inverse_cov_rotations(cov3D, rotation_matrices)
            opacity = opacity_render
            shs = shs_render
        
            if preprocessing_params["sim_area"] is not None:
                pos = torch.cat([pos, unselected_pos], dim=0)
                cov3D = torch.cat([cov3D, unselected_cov], dim=0)
                opacity = torch.cat([opacity_render, unselected_opacity], dim=0)
                shs = torch.cat([shs_render, unselected_shs], dim=0)
            
            normals = get_normals_from_cov(pos, cov3D, current_camera.camera_center)
            rgb_precomp = convert_SH(shs, current_camera, gaussians, pos, rot)

            colors_precomp = torch.cat([rgb_precomp, normals, init_refl], dim=-1)
            # print("colors_precomp shape:", colors_precomp.shape)
            
            imH = int(current_camera.image_height)
            imW = int(current_camera.image_width)
            bg_const = background[:, None, None].cuda().expand(3, imH, imW)
            bg_map = torch.cat([bg_const, torch.zeros(4,imH,imW, device='cuda')], dim=0)

            out_ts, radii = rasterize(
                means3D = pos,
                means2D = init_screen_points,
                shs = None,
                colors_precomp = colors_precomp,
                opacities = opacity,
                scales = None,
                rotations = None,
                cov3D_precomp = cov3D,
                bg_map = bg_map)
            
            base_color = out_ts[:3,...] # 3,H,W
            refl_strength = out_ts[6:7,...] #
            normal_map = out_ts[3:6,...] 
            

            normal_clone = normal_map.clone()


            normal_map = normal_map.permute(1,2,0)
            # normal_map = normal_map / (torch.norm(normal_map, dim=-1, keepdim=True)+1e-6) # in my experiments it seems that normalized normal map will cause error, but dont know why for now.



            refl_color = get_refl_color(gaussians.get_envmap, current_camera.HWK, current_camera.R, current_camera.T, normal_map)
            
            final_image = (1-refl_strength) * base_color + refl_strength * refl_color
                            
            to_save = [base_color, refl_color, normal_clone, final_image]
            paths = [base_color_path, refl_path, normap_path, final_image_path]
            names = ["base_color", "refl_color", "normal_map", "final_image"]

            for i in range(4):
                rendering = to_save[i]
                cv2_img = rendering.permute(1, 2, 0).detach().cpu().numpy()
                cv2_img = cv2.cvtColor(cv2_img, cv2.COLOR_BGR2RGB)
                if height is None or width is None:
                    height = cv2_img.shape[0] // 2 * 2
                    width = cv2_img.shape[1] // 2 * 2
                assert args.output_path is not None
                cv2.imwrite(
                    os.path.join(paths[i], f"{frame}.png".rjust(8, "0")),
                    255 * cv2_img,
                )

    if args.compile_video and args.render_img:
        fps = int(1.0 / time_params["frame_dt"])
        for i in range(4):
            os.system(
                f"ffmpeg -framerate {fps} -i {paths[i]}/%04d.png -c:v libx264 -s {width}x{height} -y -pix_fmt yuv420p {args.output_path}/{names[i]}.mp4"
            )
