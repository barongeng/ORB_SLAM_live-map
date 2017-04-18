import argparse
from config import ORB_SLAM2_DIR
from PIL import Image
import numpy as np
from config import ORB_SLAM2_DIR, KEYFRAME_DIR, MAPPOINT_DIR
from utils import read_until_good, read_keyframes, read_mappoints, get_num_cols_rows, get_graph_segment_for_frame, load_frame
import os
import matplotlib.pyplot as plt
import cv2
import time
import shutil
from point_cloud import convert_3d_point_to_pix, convert_pix_to_3d_point, get_camera_to_world, get_world_to_camera
from fit_planes import best_fitting_plane, fit_plane_with_outlier_removed
from config import cx, cy, fx, fy, k1, k2, p1, p2, k3
from scipy.signal import convolve2d

def save_map(MAP_SAVE, threshold, origins, resolution):

  map_out = Image.fromarray(((1 - output) * 255).astype(np.uint8))
  map_out.save(MAP_SAVE + '.pgm')

  with open(MAP_SAVE + '.yaml', 'wb') as f:
    f.write('image: map.pgm\n')
    f.write('resolution: {}\n'.format(resolution))
    f.write('origin: [{} {} 0.0]\n'.format(origins[0], origins[1]))
    f.write('occupied_thresh: {}\n'.format(threshold[1]))
    f.write('free_thresh: 0.165\n'.format(threshold[0]))
    f.write('negate: 0\n')


def read_kf_and_mps():
  kfs, ended = read_until_good(os.path.join(MAPPOINT_DIR, 'KeyFrames.txt'), read_keyframes)
  if ended:
    time.sleep(1)
    mps, _ = read_until_good(os.path.join(MAPPOINT_DIR, 'AllMapPoints.txt'), read_mappoints)
  else:
    #mps = np.array(('0', 0, 0, 0, 0),dtype=[('time', '|S17'), ('id', '>f8'), ('x', '>f8'), ('y', '>f8'), ('z', '>f8')])
    mps = None
    for j, kf in enumerate(kfs):
      fn = os.path.join(MAPPOINT_DIR, kf[0])
      if os.path.exists(fn):
        mps_, _ = read_until_good(fn, read_mappoints)
        if mps is None:
          mps = mps_
        else:
          mps = np.concatenate((mps, mps_), axis=0)
  kfs_dict = {}
  for kf in kfs:
    kfs_dict[kf[0]] = kf
  return kfs, mps, kfs_dict, ended

def draw_points(img, points, color):
  for point in points:
    cv2.circle(img, (point[0], point[1]), 5, color)

def get_increment_vector(dx, dy, dz, resolution):
  sign_dx = np.sign(dx)
  sign_dy = np.sign(dy)
  sign_dz = np.sign(dz)
  dx = abs(dx)
  dy = abs(dy)
  dz = abs(dz)
  if dx == 0:
    return [0 * sign_dx, resolution * sign_dy * int(dy != 0), resolution * sign_dz]
  if dz == 0:
    return [resolution * sign_dx, resolution * sign_dy * int(dy != 0), 0 * sign_dz]
  if dx > dz:
    return [resolution * sign_dx, dy * resolution / dx * sign_dy, dz * resolution / dx * sign_dz]
  else:
    return [dx * resolution / dz * sign_dx, dy * resolution / dz * sign_dy, resolution * sign_dz]


def update_map(occupied, counts, kfs_id, kfs_dict, mps, origins, resolution, y_check=None, only_end_point=0):
  numCols, numRows = get_num_cols_rows(origins, resolution)
  for mp in mps:
    if not mp[0] in kfs_id:
      continue
    px = mp[2]
    py = mp[3]
    pz = mp[4]
    kf = kfs_dict[mp[0]]
    kx = kf[1]
    ky = kf[2]
    kz = kf[3]
    dx = px - kx
    dy = py - ky
    dz = pz - kz

    u, t, v = get_increment_vector(dx, dy, dz, resolution)
    coord_end_x = int((px - origins[0]) / resolution)
    coord_end_z = numRows - int((pz - origins[1]) / resolution) - 1
    if not (coord_end_x < 0 or coord_end_x >= numCols or coord_end_z < 0 or coord_end_z >= numRows):
      counts[coord_end_z, coord_end_x] += 1
      occupied[coord_end_z, coord_end_x] += 1

    if not only_end_point:
      x = kx
      z = kz
      y = 0
      while 1:
        if (px - kx) * (px - x) < 0 or (pz - kz) * (pz - z) < 0:
          break
        if y_check is not None and (y < y_check[0] or y > y_check[1]):
          break
        coord_x = int((x - origins[0]) / resolution)
        coord_z = numRows - int((z - origins[1]) / resolution) - 1
        if coord_x < 0 or coord_x >= numCols or coord_z < 0 or coord_z >= numRows:
          break
        counts[coord_z, coord_x] += 1
        x += u
        z += v
        y += t


def create_plane_equation_dict(segment, choosen_mps, pix_of_mps):
  classes = np.unique(segment)
  point_dict = {}
  plane_equation_dict = {}

  for cl in classes:
    point_dict[cl] = []
    plane_equation_dict[cl] = None

  for j in range(len(choosen_mps)):
    u, v = pix_of_mps[j]
    world_coord = choosen_mps[j]
    cl = segment[v, u]
    point_dict[cl].append(world_coord)

  for cl in classes:
    points = point_dict[cl]
    if len(points) >= 3:
      plane = fit_plane_with_outlier_removed(points)
      plane_equation_dict[cl] = plane
  return plane_equation_dict

def create_extra_mps_from_plane_equation_dict(rows, cols, plane_equation_dict, segment, camera_to_world, world_to_camera, kf_id, fx, fy, cx, cy, im_size=[480, 640]):
  # us, vs are rows and cols respectively
  mps = []
  for row, col in zip(rows, cols):
    cl = segment[row, col]
    x, y = col, row
    plane_equation = plane_equation_dict[cl]
    if plane_equation is not None:
      X, Y, Z = convert_pix_to_3d_point(x, y, plane_equation, camera_to_world, fx, fy, cx, cy)
      x_, y_ = convert_3d_point_to_pix(X, Y, Z, world_to_camera, fx, fy, cx, cy)
      a, b, c, d = plane_equation
      print((x_, y_), (x, y), a * X + b * Y + c * Z + d)
      mps.append([kf_id, -1, X, Y, Z])
  return mps


def parse_args():
  parser = argparse.ArgumentParser()
  parser.add_argument('--path', default=ORB_SLAM2_DIR)
  parser.add_argument('--xmin', default=-5, type=float)
  parser.add_argument('--zmin', default=-5, type=float)
  parser.add_argument('--xmax', default=5, type=float)
  parser.add_argument('--zmax', default=5, type=float)
  parser.add_argument('--reso', default=0.01, type=float)
  parser.add_argument('--thresmin', default=0.165, type=float)
  parser.add_argument('--thresmax', default=0.65, type=float)
  parser.add_argument('--save', default='output/map', type=str)
  parser.add_argument('--offline', default=1, type=int)
  parser.add_argument('--min_num', default=0, type=int)
  parser.add_argument('--livemap', default=1, type=int)
  parser.add_argument('--only_end_points', default=0, type=int)
  parser.add_argument('--with_orb_slam_points', default=1, type=int)
  parser.add_argument('--with_extra_points', default=0, type=int)
  parser.add_argument('--postprocess', default=0, type=int)
  parser.add_argument('--clean_after_iteration', default=0, type=int)
  return parser.parse_args()

args = parse_args()
if not args.offline:
  try:
    shutil.rmtree(MAPPOINT_DIR)
    os.mkdir(MAPPOINT_DIR)
    shutil.rmtree(KEYFRAME_DIR)
    os.mkdir(KEYFRAME_DIR)
  except:
    pass
origins = [args.xmin, args.zmin, args.xmax, args.zmax]
resolution = args.reso
threshold = [args.thresmin, args.thresmax]
numCols, numRows = get_num_cols_rows(origins, resolution)

counts = np.ones((numRows, numCols), dtype=float) * 0
occupied = np.ones((numRows, numCols), dtype=float) * 0

grid_size = 50
im_size = [480, 640]
# rows_range = np.arange(0, im_size[0], grid_size)
rows_range = np.arange(150, 301, grid_size)
cols_range = np.arange(0, im_size[1], grid_size)
rows, cols = np.meshgrid(rows_range, cols_range)
rows, cols = rows.flatten(), cols.flatten()

processed_kfs = []
while 1:
  print('Waiting for frames')
  kfs, mps, kfs_dict, ended = read_kf_and_mps()
  if ended:
    print("ORB_SLAM ended")
    processed_kfs = []
    counts = np.ones((numRows, numCols), dtype=float) * 0
    occupied = np.ones((numRows, numCols), dtype=float) * 0
  print('Map Points:', len(mps))
  print('Key Frames:', len(kfs))

  kfs_id = []
  kfs_mappoints = {}
  for kf in kfs:
    if kf[0] not in processed_kfs:
      kfs_id.append(kf[0])
      kfs_mappoints[kf[0]] = []
      processed_kfs.append(kf[0])

  for mp in mps:
    if mp[0] in kfs_id:
      kfs_mappoints[mp[0]].append(mp)

  for kf_id in kfs_id:
    extra_mps = []
    if args.with_extra_points:
      kf = kfs_dict[kf_id]  # current keyframe
      camera_center = [kf[1], kf[2], kf[3]]
      camera_translation = [kf[4], kf[5], kf[6]]
      camera_rotation = [kf[7], kf[8], kf[9], kf[10], kf[11], kf[12], kf[13], kf[14], kf[15]]
      time_stamp = kf[0]

      camera_matrix = np.asarray([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
      mps_of_this_kf = kfs_mappoints[time_stamp]  # map points of current keyframe
      camera_to_world = get_camera_to_world(camera_translation, camera_rotation)  # tf matrix
      world_to_camera = get_world_to_camera(camera_translation, camera_rotation)  # inverse tf matrix

      kf_img = load_frame(time_stamp, 0)
      segment = get_graph_segment_for_frame(time_stamp, 0)  # segmentation from graph-cut algorithm
      segment_with_color = segment
      segment = segment[:, :, 0] * (255**2) + segment[:, :, 1] * 255 + segment[:, :, 2]
      # get pixel coordinates of the map points

      pix_of_mps = []
      choosen_mps = []
      for mp in mps_of_this_kf:
        x, y = convert_3d_point_to_pix(mp[2], mp[3], mp[4], world_to_camera, fx, fy, cx, cy)
        if x in range(0, segment.shape[1]) and y in range(0, segment.shape[0]):
          pix_of_mps.append([x, y])
          choosen_mps.append([mp[2], mp[3], mp[4]])
      pix_of_mps = np.asarray(pix_of_mps)
      plane_equation_dict = create_plane_equation_dict(segment, choosen_mps, pix_of_mps)

      extra_mps = create_extra_mps_from_plane_equation_dict(rows, cols, plane_equation_dict, segment, camera_to_world,
                                                            world_to_camera, kf_id, fx, fy, cx, cy, im_size)
      pix_of_extra_mps = []
      for mp in extra_mps:
        x, y = convert_3d_point_to_pix(mp[2], mp[3], mp[4], world_to_camera, fx, fy, cx, cy)
        if x in range(0, segment.shape[1]) and y in range(0, segment.shape[0]):
          pix_of_extra_mps.append([x, y])

      print("Extra Map Points: ", len(extra_mps))

      draw_points(segment_with_color, np.asarray(pix_of_mps, dtype=int), (0, 0, 255))
      draw_points(segment_with_color, np.asarray(pix_of_extra_mps, dtype=int), (255, 0, 0))
      cv2.imshow('Map Point Projection', segment_with_color)
      cv2.waitKey(20)

    if args.clean_after_iteration:
      counts = np.ones((numRows, numCols), dtype=float) * 0
      occupied = np.ones((numRows, numCols), dtype=float) * 0
    if args.with_orb_slam_points:
      update_map(occupied, counts, [kf_id], kfs_dict, mps, origins, resolution, only_end_point=args.only_end_points)
    update_map(occupied, counts, [kf_id], kfs_dict, extra_mps, origins, resolution, only_end_point=args.only_end_points)

    if not args.only_end_points:
      occupied[counts < args.min_num] = 0
      counts[counts < args.min_num] = 0
    if args.postprocess:
      conv_kernel = np.ones((3, 3))
      conv_kernel = conv_kernel / conv_kernel.sum()
      occupied = convolve2d(occupied, conv_kernel,'same')
      counts = convolve2d(counts, conv_kernel, 'same')
    output = occupied / counts
    output[counts == 0] = threshold[0] + 0.1
    output[output > threshold[1]] = 1.0
    output[output < threshold[0]] = 0.0
    output[(output != 1.0) * (output != 0.0)] = threshold[0] + 0.1
    save_map(args.save, threshold, origins, resolution)
    if args.livemap:
      cv2.imshow('Occupancy Grid Map', 1 - output)
      cv2.waitKey(20)
    print("Processed frame: {}".format(kf_id))
  if ended:
    cv2.waitKey(0)

# KITT0:
# python main.py --xmin -25 --xmax 20 --zmin -10 --zmax 40 --reso 0.1 --only_end_points 0
# python main.py --xmin -10 --xmax 10 --zmin -10 --zmax 10 --reso 0.05 --only_end_points 1

