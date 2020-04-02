import os, sys
import numpy as np
import imageio
import json
import random
import time
import torch
import torch.nn as nn
import torch.nn.functional as F

import matplotlib.pyplot as plt

from run_nerf_helpers_torch import *

from load_llff import load_llff_data
from load_deepvoxels import load_dv_data
from load_blender_torch import load_blender_data


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.set_default_tensor_type('torch.cuda.FloatTensor')


def batchify(fn, chunk):
    if chunk is None:
        return fn
    def ret(inputs):
        return torch.cat([fn(inputs[i:i+chunk]) for i in range(0, inputs.shape[0], chunk)], 0)
    return ret


def run_network(inputs, viewdirs, fn, embed_fn, embeddirs_fn, netchunk=1024*64):
    
    inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])
    embedded = embed_fn(inputs_flat)

    if viewdirs is not None:
        input_dirs = viewdirs[:,None].expand(inputs.shape)
        input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]])
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat([embedded, embedded_dirs], -1)
        
    outputs_flat = batchify(fn, netchunk)(embedded)
    outputs = torch.reshape(outputs_flat, list(inputs.shape[:-1]) + [outputs_flat.shape[-1]])
    return outputs


def batchify_rays(rays_flat, chunk=1024*32, **kwargs):
    
    all_ret = {}
    for i in range(0, rays_flat.shape[0], chunk):
        ret = render_rays(rays_flat[i:i+chunk], **kwargs)
        for k in ret:
            if k not in all_ret:
                all_ret[k] = []
            all_ret[k].append(ret[k])
            
    all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}
    return all_ret


def render(H, W, focal, chunk=1024*32, rays=None, c2w=None, ndc=True, 
                  near=0., far=1., 
                  use_viewdirs=False, c2w_staticcam=None,
                  **kwargs):
    
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(H, W, focal, c2w)
    else:
        # use provided ray batch
        rays_o, rays_d = rays
        
    if use_viewdirs:
        # provide ray directions as input 
        viewdirs = rays_d
        if c2w_staticcam is not None:
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(H, W, focal, c2w_staticcam)
        viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
        viewdirs = torch.reshape(viewdirs, [-1,3]).float()
    
    sh = rays_d.shape # [..., 3]
    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, focal, 1., rays_o, rays_d)
    
    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1,3]).float()
    rays_d = torch.reshape(rays_d, [-1,3]).float()
    near, far = near * torch.ones_like(rays_d[...,:1]), far * torch.ones_like(rays_d[...,:1])
    rays = torch.cat([rays_o, rays_d, near, far], -1)
    if use_viewdirs:
        rays = torch.cat([rays, viewdirs], -1)
        
    # Render and reshape
    all_ret = batchify_rays(rays, chunk, **kwargs)
    for k in all_ret:
        k_sh = list(sh[:-1]) + list(all_ret[k].shape[1:])
        all_ret[k] = torch.reshape(all_ret[k], k_sh)
    
    k_extract = ['rgb_map', 'disp_map', 'acc_map']
    ret_list = [all_ret[k] for k in k_extract]
    ret_dict = {k : all_ret[k] for k in all_ret if k not in k_extract}
    return ret_list + [ret_dict]


def render_path(render_poses, hwf, chunk, render_kwargs, gt_imgs=None, savedir=None, render_factor=0):
    
    H, W, focal = hwf
    
    if render_factor!=0:
        # Render downsampled for speed
        H = H//render_factor
        W = W//render_factor
        focal = focal/render_factor

    rgbs = []
    disps = []
    
    t = time.time()
    for i, c2w in enumerate(render_poses):
        print(i, time.time() - t) 
        t = time.time()
        rgb, disp, acc, _ = render(H, W, focal, chunk=chunk, c2w=c2w[:3,:4], **render_kwargs)
        rgbs.append(rgb.numpy())
        disps.append(disp.numpy())
        if i==0:
            print(rgb.shape, disp.shape)
                
        if gt_imgs is not None and render_factor==0:
            p = -10. * np.log10(np.mean(np.square(rgb - gt_imgs[i])))
            print(p)
            
        if savedir is not None:
            rgb8 = to8b(rgbs[-1])
            filename = os.path.join(savedir, '{:03d}.png'.format(i))
            imageio.imwrite(filename, rgb8)


    rgbs = np.stack(rgbs, 0)
    disps = np.stack(disps, 0)
    
    return rgbs, disps


def create_nerf(args):
    embed_fn, input_ch = get_embedder(args.multires, args.i_embed)

    input_ch_views = 0
    embeddirs_fn = None
    if args.use_viewdirs:
        embeddirs_fn, input_ch_views = get_embedder(args.multires_views, args.i_embed)
    output_ch = 5 if args.N_importance > 0 else 4
    skips = [4]
    model = NeRF(D=args.netdepth, W=args.netwidth, 
                 input_ch=input_ch, output_ch=output_ch, skips=skips,
                 input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs).to(device)
    grad_vars = list(model.parameters())
    models = {'model' : model}
    
            
    model_fine = None
    if args.N_importance > 0:
        model_fine = NeRF(D=args.netdepth_fine, W=args.netwidth_fine, 
                          input_ch=input_ch, output_ch=output_ch, skips=skips,
                          input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs).to(device)
        grad_vars += list(model_fine.parameters())
        models['model_fine'] = model_fine
            
    network_query_fn = lambda inputs, viewdirs, network_fn : run_network(inputs, viewdirs, network_fn,
                                                                embed_fn=embed_fn, 
                                                                embeddirs_fn=embeddirs_fn,
                                                                netchunk=args.netchunk)
        
    render_kwargs_train = {
        'network_query_fn' : network_query_fn,
        'perturb' : args.perturb,
        'N_importance' : args.N_importance,
        'network_fine' : model_fine,
        'N_samples' : args.N_samples,
        'network_fn' : model,
        'use_viewdirs' : args.use_viewdirs,
        'white_bkgd' : args.white_bkgd,
        'raw_noise_std' : args.raw_noise_std,
    }
    
    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != 'llff' or args.no_ndc:
        print('Not ndc!')
        render_kwargs_train['ndc'] = False           
        render_kwargs_train['lindisp'] = args.lindisp
        
    render_kwargs_test = {k : render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.
    
    start = 0
    basedir = args.basedir
    expname = args.expname
    
    if args.ft_path is not None and args.ft_path!='None':
        ckpts = [args.ft_path]
    else:
        ckpts = [os.path.join(basedir, expname, f) for f in sorted(os.listdir(os.path.join(basedir, expname))) if
             ('model_' in f and 'fine' not in f and 'optimizer' not in f)]
    print('Found ckpts', ckpts)
    if len(ckpts) > 0 and not args.no_reload:
        ft_weights = ckpts[-1]
        print('Reloading from', ft_weights)
        model = torch.load(ft_weights)
        if model_fine is not None:
            ft_weights_fine = '{}_fine_{}'.format(ft_weights[:-11], ft_weights[-10:])
            print('Reloading fine from', ft_weights_fine)
            model_fine = torch.load(ft_weights_fine)
        
    return render_kwargs_train, render_kwargs_test, start, grad_vars, models


def raw2outputs(raw, z_vals, rays_d, raw_noise_std=0, white_bkgd=False, pytest=False, noise_tf=None):
    """ A helper function for `render_rays`.
    """
    raw2alpha = lambda raw, dists, act_fn=F.relu: 1.-torch.exp(-act_fn(raw)*dists)
    
    dists = z_vals[...,1:] - z_vals[...,:-1]
    dists = torch.cat([dists, torch.Tensor([1e10]).expand(dists[...,:1].shape)], -1)  # [N_rays, N_samples]
    
    dists = dists * torch.norm(rays_d[...,None,:], dim=-1)

    rgb = torch.sigmoid(raw[...,:3])  # [N_rays, N_samples, 3]
    noise = 0.
    if raw_noise_std > 0.:
        noise = torch.randn(raw[...,3].shape) * raw_noise_std
        
        # Overwrite randomly sampled data if pytest
        if pytest:
            assert noise_tf is not None
            noise = noise_tf

    alpha = raw2alpha(raw[...,3] + noise, dists)  # [N_rays, N_samples]
    # weights = alpha * tf.math.cumprod(1.-alpha + 1e-10, -1, exclusive=True)
    weights = alpha * torch.cumprod(torch.cat([torch.ones((alpha.shape[0], 1)), 1.-alpha + 1e-10], -1), -1)[:, :-1]
    rgb_map = torch.sum(weights[...,None] * rgb, -2)  # [N_rays, 3]
    
    depth_map = torch.sum(weights * z_vals, -1) 
    disp_map = 1./torch.max(1e-10 * torch.ones_like(depth_map), depth_map / torch.sum(weights, -1))
    acc_map = torch.sum(weights, -1)
    
    if white_bkgd:
        rgb_map = rgb_map + (1.-acc_map[...,None])
    
    return rgb_map, disp_map, acc_map, weights, depth_map


def render_rays(ray_batch, 
                network_fn, 
                network_query_fn,
                N_samples, 
                retraw=False, 
                lindisp=False,
                perturb=0.,
                N_importance=0, 
                network_fine=None,
                white_bkgd=False,
                raw_noise_std=0.,
                verbose=False,
                pytest=False,
                t_rand_tf=None,
                z_samples_tf=None,
                noise_tf=None):     
    N_rays = ray_batch.shape[0]
    rays_o, rays_d = ray_batch[:,0:3], ray_batch[:,3:6] # [N_rays, 3] each
    viewdirs = ray_batch[:,-3:] if ray_batch.shape[-1] > 8 else None
    bounds = torch.reshape(ray_batch[...,6:8], [-1,1,2])
    near, far = bounds[...,0], bounds[...,1] # [-1,1]    
    
    t_vals = torch.linspace(0., 1., steps=N_samples)
    if not lindisp:
        z_vals = near * (1.-t_vals) + far * (t_vals) 
    else:
        z_vals = 1./(1./near * (1.-t_vals) + 1./far * (t_vals))
        
    z_vals = z_vals.expand([N_rays, N_samples])
        
    if perturb > 0.:
        # get intervals between samples     
        mids = .5 * (z_vals[...,1:] + z_vals[...,:-1])
        upper = torch.cat([mids, z_vals[...,-1:]], -1)
        lower = torch.cat([z_vals[...,:1], mids], -1)
        # stratified samples in those intervals
        t_rand = torch.rand(z_vals.shape)
        
        # Overwrite randomly sampled data if in pytest mode 
        if pytest:
            assert t_rand_tf is not None
            t_rand = t_rand_tf
        
        z_vals = lower + (upper - lower) * t_rand
        
    pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples, 3]
    
        
#     raw = run_network(pts)
    raw = network_query_fn(pts, viewdirs, network_fn)
    rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd)
    
    if N_importance > 0:
        
        rgb_map_0, disp_map_0, acc_map_0 = rgb_map, disp_map, acc_map

        z_vals_mid = .5 * (z_vals[...,1:] + z_vals[...,:-1])
        z_samples = sample_pdf(z_vals_mid, weights[...,1:-1], N_importance, det=(perturb==0.))
        z_samples = z_samples.detach()

        # Overwrite randomly sampled data if in pytest mode 
        if pytest:
            assert z_samples_tf is not None
            z_samples = z_samples_tf
        
        z_vals, _ = torch.sort(torch.cat([z_vals, z_samples], -1), -1)
        pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples + N_importance, 3]
        
        run_fn = network_fn if network_fine is None else network_fine
#         raw = run_network(pts, fn=run_fn)
        raw = network_query_fn(pts, viewdirs, run_fn)
        
        # Overwrite randomly sampled data if in pytest mode 
        if not pytest:
            rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd)
        else:
            assert noise_tf is not None
            rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, 
                                                                         pytest=True, noise_tf=noise_tf)
        
    ret = {'rgb_map' : rgb_map, 'disp_map' : disp_map, 'acc_map' : acc_map}
    if retraw:
        ret['raw'] = raw
    if N_importance > 0:
        ret['rgb0'] = rgb_map_0
        ret['disp0'] = disp_map_0
        ret['acc0'] = acc_map_0
        ret['z_std'] = torch.std(z_samples, dim=-1, unbiased=False)  # [N_rays]
        
    for k in ret:
        if torch.isnan(ret[k]).any() or torch.isinf(ret[k]).any():
            print(f"Numerical Error: output {k}: {ret[k]}")
        
    return ret


def config_parser():
    
    import configargparse
    parser = configargparse.ArgumentParser()
    parser.add_argument('--config', is_config_file=True, help='config file path')
    parser.add_argument("--expname", type=str, help='experiment name')
    parser.add_argument("--basedir", type=str, default='./logs/', help='where to store ckpts and logs')
    parser.add_argument("--datadir", type=str, default='./data/llff/fern', help='input data directory')
    
    # training options
    parser.add_argument("--netdepth", type=int, default=8, help='layers in network')
    parser.add_argument("--netwidth", type=int, default=256, help='channels per layer')
    parser.add_argument("--netdepth_fine", type=int, default=8, help='layers in fine network')
    parser.add_argument("--netwidth_fine", type=int, default=256, help='channels per layer in fine network')
    parser.add_argument("--N_rand", type=int, default=32*32*4, help='batch size (number of random rays per gradient step)')
    parser.add_argument("--lrate", type=float, default=5e-4, help='learning rate')
    parser.add_argument("--lrate_decay", type=int, default=250, help='exponential learning rate decay (in 1000 steps)')
    parser.add_argument("--chunk", type=int, default=1024*32, help='number of rays processed in parallel, decrease if running out of memory')
    parser.add_argument("--netchunk", type=int, default=1024*64, help='number of pts sent through network in parallel, decrease if running out of memory')
    parser.add_argument("--no_batching", action='store_true', help='only take random rays from 1 image at a time') 
    parser.add_argument("--no_reload", action='store_true', help='do not reload weights from saved ckpt') 
    parser.add_argument("--ft_path", type=str, default=None, help='specific weights npy file to reload for coarse network')
    
    # rendering options
    parser.add_argument("--N_samples", type=int, default=64, help='number of coarse samples per ray')
    parser.add_argument("--N_importance", type=int, default=0, help='number of additional fine samples per ray')
    parser.add_argument("--perturb", type=float, default=1., help='set to 0. for no jitter, 1. for jitter')
    parser.add_argument("--use_viewdirs", action='store_true', help='use full 5D input instead of 3D') 
    parser.add_argument("--i_embed", type=int, default=0, help='set 0 for default positional encoding, -1 for none')
    parser.add_argument("--multires", type=int, default=10, help='log2 of max freq for positional encoding (3D location)')
    parser.add_argument("--multires_views", type=int, default=4, help='log2 of max freq for positional encoding (2D direction)')
    parser.add_argument("--raw_noise_std", type=float, default=0., help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    
    parser.add_argument("--render_only", action='store_true', help='do not optimize, reload weights and render out render_poses path') 
    parser.add_argument("--render_test", action='store_true', help='render the test set instead of render_poses path') 
    parser.add_argument("--render_factor", type=int, default=0, help='downsampling factor to speed up rendering, set 4 or 8 for fast preview')
    
    # dataset options
    parser.add_argument("--dataset_type", type=str, default='llff', help='options: llff / blender / deepvoxels')
    parser.add_argument("--testskip", type=int, default=8, help='will load 1/N images from test/val sets, useful for large datasets like deepvoxels')
    
    ## deepvoxels flags
    parser.add_argument("--shape", type=str, default='greek', help='options : armchair / cube / greek / vase')
    
    ## blender flags
    parser.add_argument("--white_bkgd", action='store_true', help='set to render synthetic data on a white bkgd (always use for dvoxels)') 
    parser.add_argument("--half_res", action='store_true', help='load blender synthetic data at 400x400 instead of 800x800')
    
    ## llff flags
    parser.add_argument("--factor", type=int, default=8, help='downsample factor for LLFF images')
    parser.add_argument("--no_ndc", action='store_true', help='do not use normalized device coordinates (set for non-forward facing scenes)') 
    parser.add_argument("--lindisp", action='store_true', help='sampling linearly in disparity rather than depth') 
    parser.add_argument("--spherify", action='store_true', help='set for spherical 360 scenes') 
    parser.add_argument("--llffhold", type=int, default=8, help='will take every 1/N images as LLFF test set, paper uses 8') 
    
    # logging/saving options
    parser.add_argument("--i_print",   type=int, default=100, help='frequency of console printout and metric loggin')
    parser.add_argument("--i_img",     type=int, default=500, help='frequency of tensorboard image logging')
    parser.add_argument("--i_weights", type=int, default=10000, help='frequency of weight ckpt saving')
    parser.add_argument("--i_testset", type=int, default=50000, help='frequency of testset saving')
    parser.add_argument("--i_video",   type=int, default=50000, help='frequency of render_poses video saving')
    
    
    return parser



def train():
    
    parser = config_parser()
    args = parser.parse_args()
    
    
    # Load data
    
    if args.dataset_type == 'llff':
        images, poses, bds, render_poses, i_test = load_llff_data(args.datadir, args.factor, 
                                                                  recenter=True, bd_factor=.75, 
                                                                  spherify=args.spherify)
        hwf = poses[0,:3,-1]
        poses = poses[:,:3,:4]
        print('Loaded llff', images.shape, render_poses.shape, hwf, args.datadir)
        if not isinstance(i_test, list):
            i_test = [i_test]
        
        if args.llffhold > 0:
            print('Auto LLFF holdout,', args.llffhold)
            i_test = np.arange(images.shape[0])[::args.llffhold]
            
        i_val = i_test
        i_train = np.array([i for i in np.arange(int(images.shape[0])) if 
                        (i not in i_test and i not in i_val)])
        
        print('DEFINING BOUNDS')
        if args.no_ndc:
            near = torch.min(bds) * .9
            far = torch.max(bds) * 1.
        else:
            near = 0.
            far = 1.
        print('NEAR FAR', near, far)
            

    elif args.dataset_type == 'blender':
        images, poses, render_poses, hwf, i_split = load_blender_data(args.datadir, args.half_res, args.testskip)
        print('Loaded blender', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split
        
        near = 2.
        far = 6.
        
        if args.white_bkgd:
            images = images[...,:3]*images[...,-1:] + (1.-images[...,-1:])
        else:
            images = images[...,:3]
        
        
    elif args.dataset_type == 'deepvoxels':
        
        images, poses, render_poses, hwf, i_split = load_dv_data(scene=args.shape, 
                                                                 basedir=args.datadir, 
                                                                 testskip=args.testskip)
        
        print('Loaded deepvoxels', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split
        
        hemi_R = np.mean(np.linalg.norm(poses[:,:3,-1], axis=-1))
        near = hemi_R-1.
        far = hemi_R+1.
        

    else:
        print('Unknown dataset type', args.dataset_type, 'exiting')
        return
    
    # Cast intrinsics to right types
    H, W, focal = hwf
    H, W = int(H), int(W)
    hwf = [H, W, focal]
    
    if args.render_test:
        render_poses = np.array(poses[i_test])

    
    # Create log dir and copy the config file
    basedir = args.basedir
    expname = args.expname
    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    f = os.path.join(basedir, expname, 'args.txt')
    with open(f, 'w') as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write('{} = {}\n'.format(arg, attr))
    if args.config is not None:
        f = os.path.join(basedir, expname, 'config.txt')
        with open(f, 'w') as file:
            file.write(open(args.config, 'r').read())
    
    
    # Create nerf model
    render_kwargs_train, render_kwargs_test, start, grad_vars, models = create_nerf(args)
    
    bds_dict = {
        'near' : near,
        'far' : far,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)
    
    
    # Short circuit if only rendering out from trained model
    if args.render_only:
        print('RENDER ONLY')
        if args.render_test:
            # render_test switches to test poses
            images = images[i_test]
        else:
            # Default is smoother render_poses path
            images = None
            
        testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('test' if args.render_test else 'path', start))
        os.makedirs(testsavedir, exist_ok=True)
        print('test poses shape', render_poses.shape)
        
        rgbs, _ = render_path(render_poses, hwf, args.chunk, render_kwargs_test, gt_imgs=images, savedir=testsavedir, render_factor=args.render_factor)
        print('Done rendering', testsavedir) 
        imageio.mimwrite(os.path.join(testsavedir, 'video.mp4'), to8b(rgbs), fps=30, quality=8)
        
        return
        
        
    # Create optimizer
    lrate = args.lrate
    optimizer = torch.optim.Adam(params=grad_vars, lr=lrate, betas=(0.9, 0.999))
    
    if args.lrate_decay > 0:
        decay_rate = 0.1
        lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=decay_rate)

    models['optimizer'] = optimizer
    global_step = 0
    
    # Prepare raybatch tensor if batching random rays
    N_rand = args.N_rand
    use_batching = not args.no_batching
    if use_batching:
        # For random ray batching
        print('get rays')
        rays = np.stack([get_rays_np(H, W, focal, p) for p in poses[:,:3,:4]], 0) # [N, ro+rd, H, W, 3]
        print('done, concats')
        rays_rgb = np.concatenate([rays, images[:,None]], 1) # [N, ro+rd+rgb, H, W, 3]
        rays_rgb = np.transpose(rays_rgb, [0,2,3,1,4]) # [N, H, W, ro+rd+rgb, 3]    
        rays_rgb = np.stack([rays_rgb[i] for i in i_train], 0) # train images only
        rays_rgb = np.reshape(rays_rgb, [-1,3,3]) # [(N-1)*H*W, ro+rd+rgb, 3]    
        rays_rgb = rays_rgb.astype(np.float32)
        print('shuffle rays')
        np.random.shuffle(rays_rgb)
        print('done')
        i_batch = 0
        
    
    N_iters = 1000000
    print('Begin')
    print('TRAIN views are', i_train)
    print('TEST views are', i_test)
    print('VAL views are', i_val)
    
    # Summary writers
    # writer = tf.contrib.summary.create_file_writer(os.path.join(basedir, 'summaries', expname))
    # writer.set_as_default()
    
    rays_rgb = torch.Tensor(rays_rgb).to(device)
    for i in range(start, N_iters):
        time0 = time.time()
        
        # Sample random ray batch
        
        if use_batching:
            # Random over all images
            batch = rays_rgb[i_batch:i_batch+N_rand] # [B, 2+1, 3*?]
            batch = torch.transpose(batch, 0, 1)
            batch_rays, target_s = batch[:2], batch[2]

            i_batch += N_rand
            if i_batch >= rays_rgb.shape[0]:
                np.random.shuffle(rays_rgb)
                i_batch = 0
            
        else:
            # Random from one image
            img_i = np.random.choice(i_train)
            target = images[img_i]
            pose = poses[img_i, :3,:4]
            
            if N_rand is not None:
                rays_o, rays_d = get_rays(H, W, focal, pose)  # (H, W, 3), (H, W, 3)
                coords = torch.stack(torch.meshgrid(torch.linspace(0, H-1, H), torch.linspace(0, W-1, W)), -1)  # (H, W, 2)
                coords = torch.reshape(coords, [-1,2])  # (H * W, 2)
                select_inds = np.random.choice(coords.shape[0], size=[N_rand], replace=False)  # (N_rand,)
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                batch_rays_torch = torch.stack([rays_o, rays_d], 0)
                target_s = target[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
        
        
        #####  Core optimization loop  #####
                    
        rgb, disp, acc, extras = render(H, W, focal, chunk=args.chunk, rays=batch_rays, 
                                                verbose=i < 10, retraw=True, 
                                                **render_kwargs_train)
        
        optimizer.zero_grad()
        img_loss = img2mse(rgb, target_s)
        trans = extras['raw'][...,-1]
        loss = img_loss 
        psnr = mse2psnr(img_loss)
        
        if 'rgb0' in extras:
            img_loss0 = img2mse(extras['rgb0'], target_s)
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)

        loss.backward()
        optimizer.step()
        
        dt = time.time()-time0
        print(f"Step: {global_step}, Loss: {loss}, Time: {dt}")
        
        #####           end            #####
        
        
        # Rest is logging
        """
        def save_weights(net, prefix, i): 
            path = os.path.join(basedir, expname, '{}_{:06d}.npy'.format(prefix, i))
            np.save(path, net.get_weights())
            print('saved weights at', path)
    
        if i%args.i_weights==0:
            for k in models:
                save_weights(models[k], k, i)
                
            
        if i%args.i_video==0 and i > 0:
            
            rgbs, disps = render_path(render_poses, hwf, args.chunk, render_kwargs_test)
            print('Done, saving', rgbs.shape, disps.shape)
            moviebase = os.path.join(basedir, expname, '{}_spiral_{:06d}_'.format(expname, i))
            imageio.mimwrite(moviebase + 'rgb.mp4', to8b(rgbs), fps=30, quality=8)
            imageio.mimwrite(moviebase + 'disp.mp4', to8b(disps / np.max(disps)), fps=30, quality=8)
            
            if args.use_viewdirs:
                render_kwargs_test['c2w_staticcam'] = render_poses[0][:3,:4]
                rgbs_still, _ = render_path(render_poses, hwf, args.chunk, render_kwargs_test)
                render_kwargs_test['c2w_staticcam'] = None
                imageio.mimwrite(moviebase + 'rgb_still.mp4', to8b(rgbs_still), fps=30, quality=8)
                
                
        if i%args.i_testset==0 and i > 0:
            testsavedir = os.path.join(basedir, expname, 'testset_{:06d}'.format(i))
            os.makedirs(testsavedir, exist_ok=True)
            print('test poses shape', poses[i_test].shape)
            render_path(poses[i_test], hwf, args.chunk, render_kwargs_test, gt_imgs=images[i_test], savedir=testsavedir)
            print('Saved test set') 
            
            

        if i%args.i_print==0 or i < 10:
            
            print(expname, i, psnr.numpy(), loss.numpy(), global_step.numpy())
            print('iter time {:.05f}'.format(dt))
            with tf.contrib.summary.record_summaries_every_n_global_steps(args.i_print):
                tf.contrib.summary.scalar('loss', loss)
                tf.contrib.summary.scalar('psnr', psnr)
                tf.contrib.summary.histogram('tran', trans)
                if args.N_importance > 0:
                    tf.contrib.summary.scalar('psnr0', psnr0)
                    

            if i%args.i_img==0:
                
                # Log a rendered validation view to Tensorboard
                img_i=np.random.choice(i_val)
                target = images[img_i]
                pose = poses[img_i, :3,:4]
                
                rgb, disp, acc, extras = render(H, W, focal, chunk=args.chunk, c2w=pose, 
                                                       **render_kwargs_test)
                
                psnr = mse2psnr(img2mse(rgb, target))

                with tf.contrib.summary.record_summaries_every_n_global_steps(args.i_img):
                    
                    tf.contrib.summary.image('rgb', to8b(rgb)[tf.newaxis])
                    tf.contrib.summary.image('disp', disp[tf.newaxis,...,tf.newaxis])
                    tf.contrib.summary.image('acc', acc[tf.newaxis,...,tf.newaxis])
                
                    tf.contrib.summary.scalar('psnr_holdout', psnr)
                    tf.contrib.summary.image('rgb_holdout', target[tf.newaxis])
                
                
                if args.N_importance > 0:

                    with tf.contrib.summary.record_summaries_every_n_global_steps(args.i_img):
                        tf.contrib.summary.image('rgb0', to8b(extras['rgb0'])[tf.newaxis])
                        tf.contrib.summary.image('disp0', extras['disp0'][tf.newaxis,...,tf.newaxis])
                        tf.contrib.summary.image('z_std', extras['z_std'][tf.newaxis,...,tf.newaxis])
        """        
                    

        global_step += 1
        if global_step % args.lrate_decay * 1000 == 0:
            lr_scheduler.step()

    
if __name__=='__main__':
    train()