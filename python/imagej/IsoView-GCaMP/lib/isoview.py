from net.imglib2.img.array import ArrayImgs
from net.imglib2.img import ImgView
from net.imglib2.util import Intervals, ImgUtil
from net.imglib2.realtransform import Scale3D, AffineTransform3D
from net.imglib2.img.io import Load
import os, re, sys
from itertools import izip, chain
from util import newFixedThreadPool, Task
from io import readFloats, writeZip, KLBLoader, TransformedLoader, ImageJLoader
from registration import computeForwardTransforms, saveMatrices, loadMatrices, asBackwardConcatTransforms
from deconvolution import multiviewDeconvolution


def deconvolveTimePoints(srcDir,
                         targetDir,
                         kernel_filepath,
                         calibration,
                         cameraTransformations,
                         params,
                         modelclass,
                         roi):
  """
     Main program entry point.
     For each time point folder TM\d+, find the KLB files of the 4 cameras,
     then register them all to camera CM01, and deconvolve CM01+CM02, and CM02+CM03,
     and store these two images in corresponding TM\d+ folders under targetDir.

     Assumes that each camera view has the same dimensions in each time point folder.
     A camera view may have dimensions different from those of the other cameras.

     Can be run as many times as necessary. Intermediate computations are saved
     as csv files (features, pointmatches and transformation matrices), and 
     the deconvolved images as well, into folder targetDir/deconvolved/ with
     a name pattern like TM\d+_CM0\d_CM0\d-deconvolved.zip
     
     srcDir: file path to a directory with TM\d+ subdirectories, one per time point.
     targetDir: file path to a directory for storing deconvolved images
                and CSV files with features, point matches and transformation matrices.
     kernel_filepath: file path to the 3D image of the point spread function (PSF),
                      which can be computed from fluorescent beads with the BigStitcher functions
                      and which must have odd dimensions.
     calibration: the array of [x, y, z] dimensions.
     camera_transformations: a map of camera index vs the 12-digit 3D affine matrices describing
                             the transform to register the camera view onto the camera at index 0.
     params: a dictionary with all the necessary parameters for peak detection, feature extraction
             finding pointmatches and transformation model estimation.
     modelclass: the class of the transformation model for registering the camera views.
     roi: the min and max coordinates for cropping the coarsely registered volumes prior to registration and deconvolution.
  """
  kernel = readFloats(kernel_filepath, [19, 19, 25], header=434)
  klb_loader = KLBLoader()

  def getCalibration(img_filename):
    return calibration

  # Regular expression pattern describing KLB files to include
  pattern = re.compile("^SPM00_TM\d+_CM(\d+)_CHN0[01]\.klb$")

  exe = newFixedThreadPool()

  # Find all time point folders with pattern TM\d{6} (a TM followed by 6 digits)
  def iterTMs():
    """ Return a generator over dicts of 4 KLB file paths for each time point. """
    for dirname in sorted(os.listdir(srcDir)):
      if not dirname.startswith("TM00"):
        continue
      filepaths = {}
      tm_dir = os.path.join(srcDir, dirname)
      for filename in os.listdir(tm_dir):
        r = re.match(pattern, filename)
        if r:
          camera_index = int(r.groups()[0])
          filepaths[camera_index].append(os.path.join(tm_dir, filename))
      yield filepaths
  
  # Validate folders
  for filepaths in iterTMs():
    if 4 != len(filepaths):
      print "Folder %s has problems: found %i KLB files in it instead of 4." % (tm_dir, len(filepaths))
      print "Address the issues and rerun."
      return

  # All OK, submit all timepoint folders for registration and deconvolution 
  
  # Prepare coarse transforms
  def prepareCoarseTransforms():
    first = iterTMs().next() # filepaths for first set of 4 KLB images
    images = [klb_loader.get(first[i]) for i in sorted(first.keys())]
    scale3D = AffineTransform3D()
    scale3D.set(calibration[0], 0.0, 0.0, 0.0,
                0.0, calibration[1], 0.0, 0.0,
                0.0, 0.0, calibration[2], 0.0)
    cmTransforms = cameraTransformations(images[0], images[1], images[2], images[3], calibration)
    cmIsotropicTransforms = []
    for camera_index in sorted(cmTransforms.keys()):
      aff = AffineTransform()
      aff.set(*cmTransforms[camera_index])
      aff.concatenate(scale3D)
      cmIsotropicTransforms.append(aff)
    return cmIsotropicTransforms
  
  cmIsotropicTransforms = prepareCoarseTransforms()
  
  # Create target folder for storing deconvolved images
  if not os.path.exists(os.path.join(targetDir, "deconvolved")):
    os.mkdir(os.path.join(targetDir, "deconvolved"))
  
  # Submit for registration + deconvolution
  # The registration uses 2 parallel threads, and deconvolution all possible available threads.
  # Cannot invoke more than one time point at a time because the deconvolution requires a lot of memory.
  for filenames in iterTMs():
    deconvolveTimePoint(filepaths, targetDir, klb_loader, getCalibration, cmIsotropicTransforms,
                        roi, params, modelclass, kernel, exe)
  
  # Register deconvolved time points
  # First, all the 0_1 deconvolutions sequentially.
  # Second, all the 2_3 onto the corresponding registered 0_1.
  deconvolved_csv_dir = os.path.join(targetDir, "deconvolved/csvs")
  if not os.path.exists(deconvolved_csv_dir):
    os.mkdir(deconvolved_csv_dir)


def registerDeconvolvedTimePoints(srcDir,
                                  targetDir,
                                  params,
                                  modelclass,
                                  exe):
  """ Can only be run after running deconvolveTimePoints, because it
      expects deconvolved images to exist under targetDir/deconvolved/,
      with a name pattern like: TM_\d+_CM0\d_CM0\d-deconvolved.zip
      
      Tests if files exist first, if not, will stop execution.
      
      Returns an imglib2 4D img with the registered deconvolved 3D stacks. """

  csv_dir = os.path.join(target_dir, "deconvolved/csvs/")
  if not os.path.exists(csv_dir):
    os.mkdir(csv_dir)
  
  deconvolved_filepaths_0_1 = []
  deconvolved_filepaths_2_3 = []
  for tm_dirname in sorted(os.listdir(srcDir)):
    if tm_dirname.startswith("TM00"):
      path_0_1 = os.path.join(targetDir, "deconvolved/" + tm_dirname + "_CM00-CM01-deconvolved.zip")
      if not os.path.exists(path_0_1):
        print "File does not exists: ", path_0_1
        return
      path_2_3 = os.path.join(targetDir, "deconvolved/" + tm_dirname + "_CM02-CM03-deconvolved.zip")
      if not os.path.exists(path_2_3):
        print "File does not exists: ", path_2_3
        return
      deconvolved_filepaths_0_1.append(path_0_1)
      deconvolved_filepaths_2_3.append(path_2_3)

  # Deconvolved images are isotropic
  def getCalibration(img_filepath):
    return [1, 1, 1]

  # Register all path_0_1 to each other, then register each path_2_3 to each path_0_1
  matrices_0_1_name = "matrices_CM00_CM01"
  if os.path.exists(os.path.join(csv_dir, matrices_0_1_name + ".csv")):
    matrices_0_1 = loadMatrices(matrices_0_1_name, csv_dir)
  else:
    # Deconvolved images are opened with ImageJ: they are zipped tiff stacks.
    matrices_0_1_fwd = computeForwardTransforms(filepaths_0_1, ImageJLoader(), getCalibration,
                                                deconvolved_csv_dir, exe, modelclass, params, exe_shutdown=False)
    matrices_0_1 = [affine.getRowPackedCopy() for affine in asBackwardConcatTransforms(matrices_0_1_fwd)]
    saveMatrices(matrices_0_1_filename, matrices_0_1, csv_dir)

  # Register each deconvolved 2_3 volume to its corresponding 0_1
  matrices_2_3_name = "matrices_CM02_CM03"
  if os.path.exists(os.path.join(csv_dir, matrices_2_3_name + ".csv")):
    matrices_2_3 = loadMatrices(matrices_2_3_name, csv_dir)
  else:
    futures = []
    for pair_filepaths in izip(filepaths_0_1, filepaths_2_3):
      futures.append(exe.submit(Task, computeForwardTransforms, pair_filepaths, ImageJLoader(), getCalibration,
                                deconcolved_csv_dir, exe, modelclass, params, exe_shutdown=False))
    matrices_2_3 = []
    # Invert and concatenate the transforms
    for i, f in enumerate(futures):
      _, matrix_2_3_fwd = f.get() # first one is the identity
      aff_0_1 = AffineTransform3D()
      aff_0_1.set(*matrices_0_1[i])
      aff_2_3 = AffineTransform3D()
      aff_2_3.set(*matrix_2_3_fwd)
      aff_2_3 = aff_2_3.inverse()
      aff_2_3.preConcatenate(aff_0_1)
      matrices_2_3.append(aff_2_3.getRowPackedCopy())
    saveMatrices(matrices_2_3_name, matrices_2_3, csv_dir)

  # Show the registered deconvolved series as a 4D volume.
  affines = []
  for pair in izip(matrices_0_1, matrices_2_3):
    for matrix in pair:
      aff = AffineTransform3D()
      aff.set(*matrix_0_1)
      affines.append(aff)

  filepaths = list(chain.from_iterable(izip(deconvolved_filepaths_0_1, deconvolved_filepaths_2_3)))
  img = Load.lazyStack(filepaths, TransformedLoader(ImageJLoader(), dict(izip(filepaths, affines))))
  return img


def deconvolveTimePoint(filepaths, targetDir, klb_loader, getCalibration,
                        cmIsotropicTransforms, roi, params, modelclass, kernel,
                        exe, write=writeZip):
  """ filepaths is a dictionary of camera index vs filepath to a KLB file.
      This function will generate two deconvolved views, one for each channel,
      where CHN00 is made of CM00 + CM01, and
            CHNO1 is made of CM02 + CM03. """

  # Step 0: create csv_dir if not there
  tm_dirname = filepaths[0][filepaths[0].rfind("_TM") + 1:filepaths[0].rfind("_CM")]
  csv_dir = os.path.join(targetDir, tm_dirname + "-csv")
  if not os.path.exists(csv_dir):
    os.mkdir(csv_dir)

  # Step 1: fine registration
  # Check if matrices exist, otherwise load them:
  matrices_name = tm_dirname + "-matrices"
  if os.path.exists(os.path.join(csv_dir, matrices_name)):
    matrices = loadMatrices(matrices_name, csv_dir)
  else:
    transforming_loader = TransformedLoader(klb_loader, dict(zip(filepaths, cmIsotropicTransforms)), roi=roi)
    futures = []
    # Each task will use two additional threads
    futures.append(exe.submit(Task, computeForwardTransforms, filepaths[:2], transforming_loader, getCalibration,
                                                              csv_dir, exe, modelclass, params, exe_shutdown=False))
    futures.append(exe.submit(Task, computeForwardTransforms, filepaths[2:], transforming_loader, getCalibration,
                                                              csv_dir, exe, modelclass, params, exe_shutdown=False))
    _, matrix_0_1 = futures[0].get() # the _ is an identity transform
    _, matrix_2_3 = futures[1].get()
    matrices = [matrix_0_1, matrix_2_3]
    saveMatrices(matrices_name, matrices, csv_dir)

  # Step 2: concatenate coarse transforms with newly computed fine transformation matrices
  def concat(aff, matrix):
    t = AffineTransform3D()
    t.set(aff)
    aff1 = AffineTransform()
    aff1.set(*matrix)
    t.preConcatenate(aff1)
    return t

  transforms = [cmIsotropicTransforms[0],
                concat(cmIsotropicTransforms[1], matrices[0]),
                cmIsotropicTransforms[2],
                concat(cmIsotropicTransforms[3], matrices[1])]
  
  transforming_loader2 = TransformedLoader(klb_loader, dict(zip(filepaths, transforms)), roi=roi)

  def intoArrayImg(index):
    img = transforming_loader2.get(filepaths[index])
    imgA = ArrayImgs.unsignedShorts(Intervals.dimensionsAsLongArray(img))
    ImgUtil.copy(ImgView.wrap(img, imgA.factory()), imgA)
    return imgA

  # Step 4: deconvolution
  def deconvolveAndSave(indices):
    name = "CM0%i-CM0%i-deconvolved" % indices
    filename = tm_dirname + "_" + name + ".zip"
    path = os.path.join(targetDir, "deconvolved/" + filename)
    if not os.path.exists(path):
      # Materialize each transfomed image view into an ArrayImg
      images = map(intoArrayImg, indices)
      # Deconvolve: merge two views into a single volume
      n_iterations = params["CM_%i_%i_n_iterations" % indices]
      img = multiviewDeconvolution(images, params["blockSize"], kernel, n_iterations, exe=exe)
<<<<<<< f44d515f9771fb7187c143ff395a9b125fe480d3
      writeZip(img, path, title=filename)
=======
      write(img, path, title=filename)
>>>>>>> lib/isoview.py: functions to register and deconvolve IsoView 4-camera 4D series.

  deconvolveAndSave([0, 1])
  deconvolveAndSave([2, 3])

  


