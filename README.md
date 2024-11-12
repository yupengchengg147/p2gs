branch main: 3dgsdr + physgaussian
for now no internal filling.  
example usage:  
python gs_simulation.py --model_path ./model_3dgsdr/ball/ --config ./config/ball_fall.json --output_path ./output/test --render_img --compile_video --white_bg --relight --hdri_path /home/pyu/local_code/MA/phys/P2GS/envmaps/high_res_envmaps_1k/museum.hdr  

#TODO: 
0. should I normalize the normal map? for now unnormalized version.
1. internal filling:  
    a. how to shade the filled particles?  how to define the "normal" proerties of the internal kernels?  
    b. still limititions of 3dgs-dr:  
    No roughness built: (https://github.com/gapszju/3DGS-DR/issues/6); Observed multi-layer local minimum still exists.
