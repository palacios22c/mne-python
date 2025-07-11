"""Helpers for various transformations."""

# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import glob
import os
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy import linalg
from scipy.spatial.distance import cdist

from ._fiff.constants import FIFF
from ._fiff.open import fiff_open
from ._fiff.tag import read_tag
from ._fiff.write import start_and_end_file, write_coord_trans
from .defaults import _handle_default
from .fixes import _get_img_fdata, jit, sph_harm_y
from .utils import (
    _check_fname,
    _check_option,
    _ensure_int,
    _import_nibabel,
    _path_like,
    _require_version,
    _validate_type,
    check_fname,
    fill_doc,
    get_subjects_dir,
    logger,
    verbose,
    wrapped_stdout,
)

# transformation from anterior/left/superior coordinate system to
# right/anterior/superior:
als_ras_trans = np.array([[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])


_str_to_frame = dict(
    isotrak=FIFF.FIFFV_COORD_ISOTRAK,
    meg=FIFF.FIFFV_COORD_DEVICE,
    mri=FIFF.FIFFV_COORD_MRI,
    mri_voxel=FIFF.FIFFV_MNE_COORD_MRI_VOXEL,
    head=FIFF.FIFFV_COORD_HEAD,
    hpi=FIFF.FIFFV_COORD_HPI,
    mni_tal=FIFF.FIFFV_MNE_COORD_MNI_TAL,
    ras=FIFF.FIFFV_MNE_COORD_RAS,
    fs_tal=FIFF.FIFFV_MNE_COORD_FS_TAL,
    ctf_head=FIFF.FIFFV_MNE_COORD_CTF_HEAD,
    ctf_meg=FIFF.FIFFV_MNE_COORD_CTF_DEVICE,
    unknown=FIFF.FIFFV_COORD_UNKNOWN,
)
_frame_to_str = {val: key for key, val in _str_to_frame.items()}

_verbose_frames = {
    FIFF.FIFFV_COORD_UNKNOWN: "unknown",
    FIFF.FIFFV_COORD_DEVICE: "MEG device",
    FIFF.FIFFV_COORD_ISOTRAK: "isotrak",
    FIFF.FIFFV_COORD_HPI: "hpi",
    FIFF.FIFFV_COORD_HEAD: "head",
    FIFF.FIFFV_COORD_MRI: "MRI (surface RAS)",
    FIFF.FIFFV_MNE_COORD_MRI_VOXEL: "MRI voxel",
    FIFF.FIFFV_MNE_COORD_CTF_DEVICE: "CTF MEG device",
    FIFF.FIFFV_MNE_COORD_CTF_HEAD: "CTF/4D/KIT head",
    FIFF.FIFFV_MNE_COORD_RAS: "RAS (non-zero origin)",
    FIFF.FIFFV_MNE_COORD_MNI_TAL: "MNI Talairach",
    FIFF.FIFFV_MNE_COORD_FS_TAL: "FS Talairach",
    # We don't use these, but keep them in case we ever want to add them.
    # FIFF.FIFFV_COORD_MRI_SLICE: "MRI slice",
    # FIFF.FIFFV_COORD_MRI_DISPLAY: "MRI display",
    # FIFF.FIFFV_MNE_COORD_FS_TAL_GTZ: "Talairach (MNI z > 0)",
    # FIFF.FIFFV_MNE_COORD_FS_TAL_LTZ: "Talairach (MNI z < 0)",
}


def _to_const(cf):
    """Convert string or int coord frame into int."""
    if isinstance(cf, str):
        if cf not in _str_to_frame:
            raise ValueError(
                f"Unknown coordinate frame {cf}, "
                'expected "' + '", "'.join(_str_to_frame.keys()) + '"'
            )
        cf = _str_to_frame[cf]
    else:
        cf = _ensure_int(cf, "coordinate frame", "a str or int")
    return int(cf)


class Transform(dict):
    """A transform.

    Parameters
    ----------
    fro : str | int
        The starting coordinate frame. See notes for valid coordinate frames.
    to : str | int
        The ending coordinate frame. See notes for valid coordinate frames.
    trans : array of shape (4, 4) | None
        The transformation matrix. If None, an identity matrix will be
        used.

    Notes
    -----
    Valid coordinate frames are ``'meg'``, ``'mri'``, ``'mri_voxel'``,
    ``'head'``, ``'mri_tal'``, ``'ras'``, ``'fs_tal'``, ``'ctf_head'``,
    ``'ctf_meg'``, ``'unknown'``.
    """

    def __init__(self, fro, to, trans=None):
        super().__init__()
        # we could add some better sanity checks here
        fro = _to_const(fro)
        to = _to_const(to)
        trans = np.eye(4) if trans is None else np.asarray(trans, np.float64)
        if trans.shape != (4, 4):
            raise ValueError(f"Transformation must be shape (4, 4) not {trans.shape}")
        self["from"] = fro
        self["to"] = to
        self["trans"] = trans

    def __repr__(self):  # noqa: D105
        with np.printoptions(suppress=True):  # suppress scientific notation
            return "<Transform | {fro}->{to}>\n{trans}".format(
                fro=_coord_frame_name(self["from"]),
                to=_coord_frame_name(self["to"]),
                trans=self["trans"],
            )

    def __eq__(self, other, rtol=0.0, atol=0.0):
        """Check for equality.

        Parameter
        ---------
        other : instance of Transform
            The other transform.
        rtol : float
            Relative tolerance.
        atol : float
            Absolute tolerance.

        Returns
        -------
        eq : bool
            True if the transforms are equal.
        """
        return (
            isinstance(other, Transform)
            and self["from"] == other["from"]
            and self["to"] == other["to"]
            and np.allclose(self["trans"], other["trans"], rtol=rtol, atol=atol)
        )

    def __ne__(self, other, rtol=0.0, atol=0.0):
        """Check for inequality.

        Parameter
        ---------
        other : instance of Transform
            The other transform.
        rtol : float
            Relative tolerance.
        atol : float
            Absolute tolerance.

        Returns
        -------
        eq : bool
            True if the transforms are not equal.
        """
        return not self == other

    @property
    def from_str(self):
        """The "from" frame as a string."""
        return _coord_frame_name(self["from"])

    @property
    def to_str(self):
        """The "to" frame as a string."""
        return _coord_frame_name(self["to"])

    @fill_doc
    @verbose
    def save(self, fname, *, overwrite=False, verbose=None):
        """Save the transform as -trans.fif file.

        Parameters
        ----------
        fname : path-like
            The name of the file, which should end in ``-trans.fif``.
        %(overwrite)s
        %(verbose)s
        """
        write_trans(fname, self, overwrite=overwrite, verbose=verbose)

    def copy(self):
        """Make a copy of the transform."""
        return deepcopy(self)


def _coord_frame_name(cframe):
    """Map integers to human-readable (verbose) names."""
    return _verbose_frames.get(int(cframe), "unknown")


def _print_coord_trans(
    t, prefix="Coordinate transformation: ", units="m", level="info"
):
    # Units gives the units of the transformation. This always prints in mm.
    log_func = getattr(logger, level)
    log_func(
        prefix
        + "{fro} -> {to}".format(
            fro=_coord_frame_name(t["from"]), to=_coord_frame_name(t["to"])
        )
    )
    for ti, tt in enumerate(t["trans"]):
        scale = 1000.0 if (ti != 3 and units != "mm") else 1.0
        text = " mm" if ti != 3 else ""
        log_func(
            f"    {tt[0]:8.6f} {tt[1]:8.6f} {tt[2]:8.6f}    {scale * tt[3]:7.2f}{text}"
        )


def _find_trans(*, trans, subject, subjects_dir=None):
    if isinstance(trans, str) and trans == "auto":
        subjects_dir = get_subjects_dir(subjects_dir, raise_error=True)
        # let's try to do this in MRI coordinates so they're easy to plot
        if subject is None:
            if "SUBJECT" in os.environ:
                subject = os.environ["SUBJECT"]
            else:
                raise ValueError(
                    "subject is None and SUBJECT environment variable not set, cannot "
                    "use trans='auto'"
                )
        glob_str = str(subjects_dir / subject / "*-trans.fif")
        trans_fnames = glob.glob(glob_str)
        if len(trans_fnames) < 1:
            raise RuntimeError(
                f"Could not find the transformation for {subject} in: {glob_str}"
            )
        elif len(trans_fnames) > 1:
            raise RuntimeError(
                f"Found multiple transformations for {subject} in: {glob_str}"
            )
        trans = Path(trans_fnames[0])
    return _get_trans(trans, fro="head", to="mri")


def apply_trans(trans, pts, move=True):
    """Apply a transform matrix to an array of points.

    Parameters
    ----------
    trans : array, shape = (4, 4) | instance of Transform
        Transform matrix.
    pts : array, shape = (3,) | (n, 3)
        Array with coordinates for one or n points.
    move : bool
        If True (default), apply translation.

    Returns
    -------
    transformed_pts : shape = (3,) | (n, 3)
        Transformed point(s).
    """
    if isinstance(trans, dict):
        trans = trans["trans"]
    pts = np.asarray(pts)
    if pts.size == 0:
        return pts.copy()

    # apply rotation & scale
    out_pts = np.dot(pts, trans[:3, :3].T)
    # apply translation
    if move:
        out_pts += trans[:3, 3]

    return out_pts


def rotation(x=0, y=0, z=0):
    """Create an array with a 4 dimensional rotation matrix.

    Parameters
    ----------
    x, y, z : scalar
        Rotation around the origin (in rad).

    Returns
    -------
    r : array, shape = (4, 4)
        The rotation matrix.
    """
    r = np.eye(4)
    r[:3, :3] = rotation3d(x=x, y=y, z=z)
    return r


def rotation3d(x=0, y=0, z=0):
    """Create an array with a 3 dimensional rotation matrix.

    Parameters
    ----------
    x, y, z : scalar
        Rotation around the origin (in rad).

    Returns
    -------
    r : array, shape = (3, 3)
        The rotation matrix.
    """
    cos_x = np.cos(x)
    cos_y = np.cos(y)
    cos_z = np.cos(z)
    sin_x = np.sin(x)
    sin_y = np.sin(y)
    sin_z = np.sin(z)
    r = np.array(
        [
            [
                cos_y * cos_z,
                -cos_x * sin_z + sin_x * sin_y * cos_z,
                sin_x * sin_z + cos_x * sin_y * cos_z,
            ],
            [
                cos_y * sin_z,
                cos_x * cos_z + sin_x * sin_y * sin_z,
                -sin_x * cos_z + cos_x * sin_y * sin_z,
            ],
            [-sin_y, sin_x * cos_y, cos_x * cos_y],
        ],
        dtype=float,
    )
    return r


def rotation3d_align_z_axis(target_z_axis):
    """Compute a rotation matrix to align [ 0 0 1] with supplied target z axis.

    Parameters
    ----------
    target_z_axis : array, shape (1, 3)
        z axis. computed matrix (r) will map [0 0 1] to target_z_axis

    Returns
    -------
    r : array, shape (3, 3)
        The rotation matrix.
    """
    target_z_axis = target_z_axis / np.linalg.norm(target_z_axis)
    r = np.zeros((3, 3))
    if (1.0 + target_z_axis[2]) < 1e-12:
        r[0, 0] = 1.0
        r[1, 1] = -1.0
        r[2, 2] = -1.0
    else:
        f = 1.0 / (1.0 + target_z_axis[2])
        r[0, 0] = 1.0 - 1.0 * f * target_z_axis[0] * target_z_axis[0]
        r[0, 1] = -1.0 * f * target_z_axis[0] * target_z_axis[1]
        r[0, 2] = target_z_axis[0]
        r[1, 0] = -1.0 * f * target_z_axis[0] * target_z_axis[1]
        r[1, 1] = 1.0 - 1.0 * f * target_z_axis[1] * target_z_axis[1]
        r[1, 2] = target_z_axis[1]
        r[2, 0] = -target_z_axis[0]
        r[2, 1] = -target_z_axis[1]
        r[2, 2] = 1.0 - f * (
            target_z_axis[0] * target_z_axis[0] + target_z_axis[1] * target_z_axis[1]
        )

    # assert that r is a rotation matrix r^t * r = I and det(r) = 1
    assert np.any((r.dot(r.T) - np.identity(3)) < 1e-12)
    assert (np.linalg.det(r) - 1.0) < 1e-12
    # assert that r maps [0 0 1] on the device z axis (target_z_axis)
    assert np.linalg.norm(target_z_axis - r.dot([0, 0, 1])) < 1e-12

    return r


def rotation_angles(m):
    """Find rotation angles from a transformation matrix.

    Parameters
    ----------
    m : array, shape >= (3, 3)
        Rotation matrix. Only the top left 3 x 3 partition is accessed.

    Returns
    -------
    x, y, z : float
        Rotation around x, y and z axes.
    """
    x = np.arctan2(m[2, 1], m[2, 2])
    c2 = np.sqrt(m[0, 0] ** 2 + m[1, 0] ** 2)
    y = np.arctan2(-m[2, 0], c2)
    s1 = np.sin(x)
    c1 = np.cos(x)
    z = np.arctan2(s1 * m[0, 2] - c1 * m[0, 1], c1 * m[1, 1] - s1 * m[1, 2])
    return x, y, z


def scaling(x=1, y=1, z=1):
    """Create an array with a scaling matrix.

    Parameters
    ----------
    x, y, z : scalar
        Scaling factors.

    Returns
    -------
    s : array, shape = (4, 4)
        The scaling matrix.
    """
    s = np.array([[x, 0, 0, 0], [0, y, 0, 0], [0, 0, z, 0], [0, 0, 0, 1]], dtype=float)
    return s


def translation(x=0, y=0, z=0):
    """Create an array with a translation matrix.

    Parameters
    ----------
    x, y, z : scalar
        Translation parameters.

    Returns
    -------
    m : array, shape = (4, 4)
        The translation matrix.
    """
    m = np.array([[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]], dtype=float)
    return m


def _ensure_trans(trans, fro="mri", to="head", *, extra=""):
    """Ensure we have the proper transform."""
    if isinstance(fro, str):
        from_str = fro
        from_const = _str_to_frame[fro]
    else:
        from_str = _frame_to_str[fro]
        from_const = fro
    del fro
    if isinstance(to, str):
        to_str = to
        to_const = _str_to_frame[to]
    else:
        to_str = _frame_to_str[to]
        to_const = to
    del to
    extra = f" {extra}" if extra else ""
    err_str = f"trans must be a Transform between {from_str}<->{to_str}{extra}, got"
    if not isinstance(trans, list | tuple):
        trans = [trans]
    # Ensure that we have exactly one match
    idx = list()
    misses = list()
    for ti, this_trans in enumerate(trans):
        if not isinstance(this_trans, Transform):
            raise ValueError(f"{err_str} None")
        if {this_trans["from"], this_trans["to"]} == {from_const, to_const}:
            idx.append(ti)
        else:
            misses += [
                "{fro}->{to}".format(
                    fro=_frame_to_str[this_trans["from"]],
                    to=_frame_to_str[this_trans["to"]],
                )
            ]
    if len(idx) != 1:
        raise ValueError(f"{err_str} " + ", ".join(misses))
    trans = trans[idx[0]]
    if trans["from"] != from_const:
        trans = invert_transform(trans)
    return trans


def _get_trans(trans, fro="mri", to="head", allow_none=True, *, extra=""):
    """Get mri_head_t (from=mri, to=head) from mri filename."""
    types = (Transform, "path-like")
    if allow_none:
        types += (None,)
    _validate_type(trans, types, "trans", extra=extra)
    if _path_like(trans):
        if trans == "fsaverage":
            trans = Path(__file__).parent / "data" / "fsaverage" / "fsaverage-trans.fif"
        trans = Path(trans)
        if not trans.is_file():
            raise OSError(f'trans file "{trans}" not found')
        if trans.suffix in [".fif", ".gz"]:
            fro_to_t = read_trans(trans)
        else:
            # convert "-trans.txt" to "-trans.fif" mri-type equivalent
            # these are usually actually in to_fro form
            t = np.genfromtxt(trans)
            if t.ndim != 2 or t.shape != (4, 4):
                raise RuntimeError(f'File "{trans}" did not have 4x4 entries')
            fro_to_t = Transform(to, fro, t)
    elif isinstance(trans, Transform):
        fro_to_t = trans
        trans = "instance of Transform"
    else:
        assert trans is None
        fro_to_t = Transform(fro, to)
        trans = "identity"
    # it's usually a head->MRI transform, so we probably need to invert it
    fro_to_t = _ensure_trans(fro_to_t, fro, to, extra=extra)
    return fro_to_t, trans


def combine_transforms(t_first, t_second, fro, to):
    """Combine two transforms.

    Parameters
    ----------
    t_first : dict
        First transform.
    t_second : dict
        Second transform.
    fro : int
        From coordinate frame.
    to : int
        To coordinate frame.

    Returns
    -------
    trans : dict
        Combined transformation.
    """
    fro = _to_const(fro)
    to = _to_const(to)
    if t_first["from"] != fro:
        raise RuntimeError(
            'From mismatch: {fro1} ("{cf1}") != {fro2} ("{cf2}")'.format(
                fro1=t_first["from"],
                cf1=_coord_frame_name(t_first["from"]),
                fro2=fro,
                cf2=_coord_frame_name(fro),
            )
        )
    if t_first["to"] != t_second["from"]:
        raise RuntimeError(
            'Transform mismatch: t1["to"] = {to1} ("{cf1}"), '
            't2["from"] = {fro2} ("{cf2}")'.format(
                to1=t_first["to"],
                cf1=_coord_frame_name(t_first["to"]),
                fro2=t_second["from"],
                cf2=_coord_frame_name(t_second["from"]),
            )
        )
    if t_second["to"] != to:
        raise RuntimeError(
            'To mismatch: {to1} ("{cf1}") != {to2} ("{cf2}")'.format(
                to1=t_second["to"],
                cf1=_coord_frame_name(t_second["to"]),
                to2=to,
                cf2=_coord_frame_name(to),
            )
        )
    return Transform(fro, to, np.dot(t_second["trans"], t_first["trans"]))


@verbose
def read_trans(fname, return_all=False, verbose=None):
    """Read a ``-trans.fif`` file.

    Parameters
    ----------
    fname : path-like
        The name of the file.
    return_all : bool
        If True, return all transformations in the file.
        False (default) will only return the first.

        .. versionadded:: 0.15
    %(verbose)s

    Returns
    -------
    trans : dict | list of dict
        The transformation dictionary from the fif file.

    See Also
    --------
    write_trans
    mne.transforms.Transform
    """
    fname = _check_fname(fname, overwrite="read", must_exist=True)
    fid, tree, directory = fiff_open(fname)

    trans = list()
    with fid:
        for t in directory:
            if t.kind == FIFF.FIFF_COORD_TRANS:
                trans.append(read_tag(fid, t.pos).data)
                if not return_all:
                    break
    if len(trans) == 0:
        raise OSError("This does not seem to be a -trans.fif file.")
    return trans if return_all else trans[0]


@verbose
def write_trans(fname, trans, *, overwrite=False, verbose=None):
    """Write a transformation FIF file.

    Parameters
    ----------
    fname : path-like
        The name of the file, which should end in ``-trans.fif``.
    trans : dict
        Trans file data, as returned by `~mne.read_trans`.
    %(overwrite)s
    %(verbose)s

    See Also
    --------
    read_trans
    """
    check_fname(
        fname, "trans", ("-trans.fif", "-trans.fif.gz", "_trans.fif", "_trans.fif.gz")
    )
    fname = _check_fname(fname=fname, overwrite=overwrite)
    with start_and_end_file(fname) as fid:
        write_coord_trans(fid, trans)


def invert_transform(trans):
    """Invert a transformation between coordinate systems.

    Parameters
    ----------
    trans : dict
        Transform to invert.

    Returns
    -------
    inv_trans : dict
        Inverse transform.
    """
    return Transform(trans["to"], trans["from"], np.linalg.inv(trans["trans"]))


def transform_surface_to(surf, dest, trans, copy=False):
    """Transform surface to the desired coordinate system.

    Parameters
    ----------
    surf : dict
        Surface.
    dest : 'meg' | 'mri' | 'head' | int
        Destination coordinate system. Can be an integer for using
        FIFF types.
    trans : dict | list of dict
        Transformation to use (or a list of possible transformations to
        check).
    copy : bool
        If False (default), operate in-place.

    Returns
    -------
    res : dict
        Transformed source space.
    """
    surf = deepcopy(surf) if copy else surf
    if isinstance(dest, str):
        if dest not in _str_to_frame:
            raise KeyError(
                f'dest must be one of {list(_str_to_frame.keys())}, not "{dest}"'
            )
        dest = _str_to_frame[dest]  # convert to integer
    if surf["coord_frame"] == dest:
        return surf

    trans = _ensure_trans(trans, int(surf["coord_frame"]), dest)
    surf["coord_frame"] = dest
    surf["rr"] = apply_trans(trans, surf["rr"])
    if "nn" in surf:
        surf["nn"] = apply_trans(trans, surf["nn"], move=False)
    return surf


def get_ras_to_neuromag_trans(nasion, lpa, rpa):
    """Construct a transformation matrix to the MNE head coordinate system.

    Construct a transformation matrix from an arbitrary RAS coordinate system
    to the MNE head coordinate system, in which the x axis passes through the
    two preauricular points, and the y axis passes through the nasion and is
    normal to the x axis. (see mne manual, pg. 97)

    Parameters
    ----------
    nasion : array_like, shape (3,)
        Nasion point coordinate.
    lpa : array_like, shape (3,)
        Left peri-auricular point coordinate.
    rpa : array_like, shape (3,)
        Right peri-auricular point coordinate.

    Returns
    -------
    trans : numpy.array, shape = (4, 4)
        Transformation matrix to MNE head space.
    """
    # check input args
    nasion = np.asarray(nasion)
    lpa = np.asarray(lpa)
    rpa = np.asarray(rpa)
    for pt in (nasion, lpa, rpa):
        if pt.ndim != 1 or len(pt) != 3:
            raise ValueError(
                "Points have to be provided as one dimensional arrays of length 3."
            )

    right = rpa - lpa
    right_unit = right / np.linalg.norm(right)

    origin = lpa + np.dot(nasion - lpa, right_unit) * right_unit

    anterior = nasion - origin
    anterior_unit = anterior / np.linalg.norm(anterior)

    superior_unit = np.cross(right_unit, anterior_unit)

    x, y, z = -origin
    origin_trans = translation(x, y, z)

    trans_l = np.vstack((right_unit, anterior_unit, superior_unit, [0, 0, 0]))
    trans_r = np.reshape([0, 0, 0, 1], (4, 1))
    rot_trans = np.hstack((trans_l, trans_r))

    trans = np.dot(rot_trans, origin_trans)
    return trans


def _get_transforms_to_coord_frame(info, trans, coord_frame="mri"):
    """Get the transforms to a coordinate frame from device, head and mri."""
    head_mri_t = _get_trans(trans, "head", "mri")[0]
    dev_head_t = _get_trans(info["dev_head_t"], "meg", "head")[0]
    mri_dev_t = invert_transform(
        combine_transforms(dev_head_t, head_mri_t, "meg", "mri")
    )
    to_cf_t = dict(
        meg=_ensure_trans(
            [dev_head_t, mri_dev_t, Transform("meg", "meg")], fro="meg", to=coord_frame
        ),
        head=_ensure_trans(
            [dev_head_t, head_mri_t, Transform("head", "head")],
            fro="head",
            to=coord_frame,
        ),
        mri=_ensure_trans(
            [head_mri_t, mri_dev_t, Transform("mri", "mri")], fro="mri", to=coord_frame
        ),
    )
    return to_cf_t


###############################################################################
# Spherical coordinates and harmonics


def _cart_to_sph(cart):
    """Convert Cartesian coordinates to spherical coordinates.

    Parameters
    ----------
    cart_pts : ndarray, shape (n_points, 3)
        Array containing points in Cartesian coordinates (x, y, z)

    Returns
    -------
    sph_pts : ndarray, shape (n_points, 3)
        Array containing points in spherical coordinates (rad, azimuth, polar)
    """
    cart = np.atleast_2d(cart)
    assert cart.ndim == 2 and cart.shape[1] == 3, cart.shape
    out = np.empty((len(cart), 3))
    out[:, 0] = np.sqrt(np.sum(cart * cart, axis=1))
    norm = np.where(out[:, 0] > 0, out[:, 0], 1)  # protect against / 0
    out[:, 1] = np.arctan2(cart[:, 1], cart[:, 0])
    out[:, 2] = np.arccos(cart[:, 2] / norm)
    out = np.nan_to_num(out)
    return out


def _sph_to_cart(sph_pts):
    """Convert spherical coordinates to Cartesian coordinates.

    Parameters
    ----------
    sph_pts : ndarray, shape (n_points, 3)
        Array containing points in spherical coordinates (rad, azimuth, polar)

    Returns
    -------
    cart_pts : ndarray, shape (n_points, 3)
        Array containing points in Cartesian coordinates (x, y, z)

    """
    sph_pts = np.atleast_2d(sph_pts)
    assert sph_pts.ndim == 2 and sph_pts.shape[1] == 3
    cart_pts = np.empty((len(sph_pts), 3))
    cart_pts[:, 2] = sph_pts[:, 0] * np.cos(sph_pts[:, 2])
    xy = sph_pts[:, 0] * np.sin(sph_pts[:, 2])
    cart_pts[:, 0] = xy * np.cos(sph_pts[:, 1])
    cart_pts[:, 1] = xy * np.sin(sph_pts[:, 1])
    return cart_pts


def _get_n_moments(order):
    """Compute the number of multipolar moments (spherical harmonics).

    Equivalent to :footcite:`DarvasEtAl2006` Eq. 32.

    .. note:: This count excludes ``degree=0`` (for ``order=0``).

    Parameters
    ----------
    order : array-like
        Expansion orders, often ``[int_order, ext_order]``.

    Returns
    -------
    M : ndarray
        Number of moments due to each order.
    """
    order = np.asarray(order, int)
    return (order + 2) * order


def _sph_to_cart_partials(az, pol, g_rad, g_az, g_pol):
    """Convert spherical partial derivatives to cartesian coords.

    Note: Because we are dealing with partial derivatives, this calculation is
    not a static transformation. The transformation matrix itself is dependent
    on azimuth and polar coord.

    See the 'Spherical coordinate sytem' section here:
    wikipedia.org/wiki/Vector_fields_in_cylindrical_and_spherical_coordinates

    Parameters
    ----------
    az : ndarray, shape (n_points,)
        Array containing spherical coordinates points (azimuth).
    pol : ndarray, shape (n_points,)
        Array containing spherical coordinates points (polar).
    sph_grads : ndarray, shape (n_points, 3)
        Array containing partial derivatives at each spherical coordinate
        (radius, azimuth, polar).

    Returns
    -------
    cart_grads : ndarray, shape (n_points, 3)
        Array containing partial derivatives in Cartesian coordinates (x, y, z)
    """
    sph_grads = np.c_[g_rad, g_az, g_pol]
    c_as, s_as = np.cos(az), np.sin(az)
    c_ps, s_ps = np.cos(pol), np.sin(pol)
    trans = np.array(
        [
            [c_as * s_ps, -s_as, c_as * c_ps],
            [s_as * s_ps, c_as, c_ps * s_as],
            [c_ps, np.zeros_like(c_as), -s_ps],
        ]
    )
    cart_grads = np.einsum("ijk,kj->ki", trans, sph_grads)
    return cart_grads


def _deg_ord_idx(deg, order):
    """Get the index into S_in or S_out given a degree and order."""
    # The -1 here is because we typically exclude the degree=0 term
    return deg * deg + deg + order - 1


def _sh_negate(sh, order):
    """Get the negative spherical harmonic from a positive one."""
    assert order >= 0
    return sh.conj() * (-1.0 if order % 2 else 1.0)  # == (-1) ** order


def _sh_complex_to_real(sh, order):
    """Convert complex to real basis functions.

    Parameters
    ----------
    sh : array-like
        Spherical harmonics. Must be from order >=0 even if negative orders
        are used.
    order : int
        Order (usually 'm') of multipolar moment.

    Returns
    -------
    real_sh : array-like
        The real version of the spherical harmonics.

    Notes
    -----
    This does not include the Condon-Shortely phase.
    """
    if order == 0:
        return np.real(sh)
    else:
        return np.sqrt(2.0) * (np.real if order > 0 else np.imag)(sh)


def _sh_real_to_complex(shs, order):
    """Convert real spherical harmonic pair to complex.

    Parameters
    ----------
    shs : ndarray, shape (2, ...)
        The real spherical harmonics at ``[order, -order]``.
    order : int
        Order (usually 'm') of multipolar moment.

    Returns
    -------
    sh : array-like, shape (...)
        The complex version of the spherical harmonics.
    """
    if order == 0:
        return shs[0]
    else:
        return (shs[0] + 1j * np.sign(order) * shs[1]) / np.sqrt(2.0)


def _compute_sph_harm(order, az, pol):
    """Compute complex spherical harmonics of spherical coordinates."""
    out = np.empty((len(az), _get_n_moments(order) + 1))
    # _deg_ord_idx(0, 0) = -1 so we're actually okay to use it here
    for degree in range(order + 1):
        for order_ in range(degree + 1):
            sph = sph_harm_y(degree, order_, pol, az)
            out[:, _deg_ord_idx(degree, order_)] = _sh_complex_to_real(sph, order_)
            if order_ > 0:
                out[:, _deg_ord_idx(degree, -order_)] = _sh_complex_to_real(
                    _sh_negate(sph, order_), -order_
                )
    return out


###############################################################################
# Thin-plate spline transformations

# Adapted from code from the MATLAB file exchange:
#    https://www.mathworks.com/matlabcentral/fileexchange/
#            53867-3d-point-set-warping-by-thin-plate-rbf-function
#    https://www.mathworks.com/matlabcentral/fileexchange/
#            53828-rbf-or-thin-plate-splines-image-warping
# Associated (BSD 2-clause) license:
#
# Copyright (c) 2015, Wang Lin
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in
#       the documentation and/or other materials provided with the distribution
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


class _TPSWarp:
    """Transform points using thin-plate spline (TPS) warping.

    Notes
    -----
    Based on the method by :footcite:`Bookstein1989` and
    adapted from code by Wang Lin (wanglin193@hotmail.com>).

    References
    ----------
    .. footbibliography::
    """

    def fit(self, source, destination, reg=1e-3):
        assert source.shape[1] == destination.shape[1] == 3
        assert source.shape[0] == destination.shape[0]
        # Forward warping, different from image warping, use |dist|**2
        dists = _tps(cdist(source, destination, "sqeuclidean"))
        # Y = L * w
        # L: RBF matrix about source
        # Y: Points matrix about destination
        P = np.concatenate((np.ones((source.shape[0], 1)), source), axis=-1)
        L = np.vstack([np.hstack([dists, P]), np.hstack([P.T, np.zeros((4, 4))])])
        Y = np.concatenate((destination, np.zeros((4, 3))), axis=0)
        # Regularize it a bit
        L += reg * np.eye(L.shape[0])
        self._destination = destination.copy()
        self._weights = linalg.lstsq(L, Y)[0]
        return self

    @verbose
    def transform(self, pts, verbose=None):
        """Apply the warp.

        Parameters
        ----------
        pts : shape (n_transform, 3)
            Source points to warp to the destination.

        Returns
        -------
        dest : shape (n_transform, 3)
            The transformed points.
        """
        logger.info(f"Transforming {len(pts)} points")
        assert pts.shape[1] == 3
        # for memory reasons, we should do this in ~100 MB chunks
        out = np.zeros_like(pts)
        n_splits = max(
            int((pts.shape[0] * self._destination.shape[0]) / (100e6 / 8.0)), 1
        )
        for this_out, this_pts in zip(
            np.array_split(out, n_splits), np.array_split(pts, n_splits)
        ):
            dists = _tps(cdist(this_pts, self._destination, "sqeuclidean"))
            L = np.hstack((dists, np.ones((dists.shape[0], 1)), this_pts))
            this_out[:] = np.dot(L, self._weights)
        assert not (out == 0).any()
        return out


def _tps(distsq):
    """Thin-plate function (r ** 2) * np.log(r)."""
    # NOTE: For our warping functions, a radial basis like
    # exp(-distsq / radius ** 2) could also be used
    out = np.zeros_like(distsq)
    mask = distsq > 0  # avoid log(0)
    valid = distsq[mask]
    out[mask] = valid * np.log(valid)
    return out


###############################################################################
# Spherical harmonic approximation + TPS warp


class _SphericalSurfaceWarp:
    """Warp surfaces via spherical harmonic smoothing and thin-plate splines.

    Notes
    -----
    This class can be used to warp data from a source subject to
    a destination subject, as described in :footcite:`DarvasEtAl2006`.

    The procedure is:

        1. Perform a spherical harmonic approximation to the source and
           destination surfaces, which smooths them and allows arbitrary
           interpolation.
        2. Choose a set of matched points on the two surfaces.
        3. Use thin-plate spline warping (common in 2D image manipulation)
           to generate transformation coefficients.
        4. Warp points from the source subject (which should be inside the
           original surface) to the destination subject.

    .. versionadded:: 0.14

    References
    ----------
    .. footbibliography::
    """

    def __repr__(self):
        rep = "<SphericalSurfaceWarp : "
        if not hasattr(self, "_warp"):
            rep += "no fitting done >"
        else:
            rep += (
                f"fit {self._fit_params['n_src']}->{self._fit_params['n_dest']} pts "
                f"using match={self._fit_params['match']} "
                f"({self._fit_params['n_match']} pts), "
                f"order={self._fit_params['order']}, reg={self._fit_params['reg']}>"
            )
        return rep

    @verbose
    def fit(
        self,
        source,
        destination,
        order=4,
        reg=1e-5,
        center=True,
        match="oct5",
        verbose=None,
    ):
        """Fit the warp from source points to destination points.

        Parameters
        ----------
        source : array, shape (n_src, 3)
            The source points.
        destination : array, shape (n_dest, 3)
            The destination points.
        order : int
            Order of the spherical harmonic fit.
        reg : float
            Regularization of the TPS warp.
        center : bool
            If True, center the points by fitting a sphere to points
            that are in a reasonable region for head digitization.
        match : str
            The uniformly-spaced points to match on the two surfaces.
            Can be "ico#" or "oct#" where "#" is an integer.
            The default is "oct5".
        %(verbose)s

        Returns
        -------
        inst : instance of SphericalSurfaceWarp
            The warping object (for chaining).
        """
        from .bem import _fit_sphere
        from .source_space._source_space import _check_spacing

        match_rr = _check_spacing(match, verbose=False)[2]["rr"]
        logger.info("Computing TPS warp")
        src_center = dest_center = np.zeros(3)
        if center:
            logger.info("    Centering data")
            hsp = np.array([p for p in source if not (p[2] < -1e-6 and p[1] > 1e-6)])
            src_center = _fit_sphere(hsp)[1]
            source = source - src_center
            hsp = np.array([p for p in destination if not (p[2] < 0 and p[1] > 0)])
            dest_center = _fit_sphere(hsp)[1]
            destination = destination - dest_center
            logger.info(
                "    Using centers {np.array_str(src_center, None, 3)} -> "
                "{np.array_str(dest_center, None, 3)}"
            )
        self._fit_params = dict(
            n_src=len(source),
            n_dest=len(destination),
            match=match,
            n_match=len(match_rr),
            order=order,
            reg=reg,
        )
        assert source.shape[1] == destination.shape[1] == 3
        self._destination = destination.copy()
        # 1. Compute spherical coordinates of source and destination points
        logger.info("    Converting to spherical coordinates")
        src_rad_az_pol = _cart_to_sph(source).T
        dest_rad_az_pol = _cart_to_sph(destination).T
        match_rad_az_pol = _cart_to_sph(match_rr).T
        del match_rr
        # 2. Compute spherical harmonic coefficients for all points
        logger.info(
            f"    Computing spherical harmonic approximation with order {order}"
        )
        src_sph = _compute_sph_harm(order, *src_rad_az_pol[1:])
        dest_sph = _compute_sph_harm(order, *dest_rad_az_pol[1:])
        match_sph = _compute_sph_harm(order, *match_rad_az_pol[1:])
        # 3. Fit spherical harmonics to both surfaces to smooth them
        src_coeffs = linalg.lstsq(src_sph, src_rad_az_pol[0])[0]
        dest_coeffs = linalg.lstsq(dest_sph, dest_rad_az_pol[0])[0]
        # 4. Smooth both surfaces using these coefficients, and evaluate at
        #     the "shape" points
        logger.info(
            f"    Matching {len(match_sph)} points ({match}) on smoothed surfaces"
        )
        src_rad_az_pol = match_rad_az_pol.copy()
        src_rad_az_pol[0] = np.abs(np.dot(match_sph, src_coeffs))
        dest_rad_az_pol = match_rad_az_pol.copy()
        dest_rad_az_pol[0] = np.abs(np.dot(match_sph, dest_coeffs))
        # 5. Convert matched points to Cartesian coordinates and put back
        source = _sph_to_cart(src_rad_az_pol.T)
        source += src_center
        destination = _sph_to_cart(dest_rad_az_pol.T)
        destination += dest_center
        # 6. Compute TPS warp of matched points from smoothed surfaces
        self._warp = _TPSWarp().fit(source, destination, reg)
        logger.info("[done]")
        return self

    @verbose
    def transform(self, source, verbose=None):
        """Transform arbitrary source points to the destination.

        Parameters
        ----------
        source : ndarray, shape (n_pts, 3)
            Source points to transform. They do not need to be the same
            points that were used to generate the model, although ideally
            they will be inside the convex hull formed by the original
            source points.
        %(verbose)s

        Returns
        -------
        destination : ndarray, shape (n_pts, 3)
            The points transformed to the destination space.
        """
        return self._warp.transform(source)


###############################################################################
# Other transforms


def _pol_to_cart(pol):
    """Transform polar coordinates to cartesian."""
    out = np.empty((len(pol), 2))
    if pol.shape[1] == 2:  # phi, theta
        out[:, 0] = pol[:, 0] * np.cos(pol[:, 1])
        out[:, 1] = pol[:, 0] * np.sin(pol[:, 1])
    else:  # radial distance, theta, phi
        d = pol[:, 0] * np.sin(pol[:, 2])
        out[:, 0] = d * np.cos(pol[:, 1])
        out[:, 1] = d * np.sin(pol[:, 1])
    return out


def _topo_to_sph(topo):
    """Convert 2D topo coordinates to spherical coordinates."""
    assert topo.ndim == 2 and topo.shape[1] == 2
    sph = np.ones((len(topo), 3))
    sph[:, 1] = -np.deg2rad(topo[:, 0])
    sph[:, 2] = np.pi * topo[:, 1]
    return sph


###############################################################################
# Quaternions


@jit()
def quat_to_rot(quat):
    """Convert a set of quaternions to rotations.

    Parameters
    ----------
    quat : array, shape (..., 3)
        The q1, q2, and q3 (x, y, z) parameters of a unit quaternion.

    Returns
    -------
    rot : array, shape (..., 3, 3)
        The corresponding rotation matrices.

    See Also
    --------
    rot_to_quat
    """
    # z = a + bi + cj + dk
    b, c, d = quat[..., 0], quat[..., 1], quat[..., 2]
    bb, cc, dd = b * b, c * c, d * d
    # use max() here to be safe in case roundoff errs put us over
    aa = np.maximum(1.0 - bb - cc - dd, 0.0)
    a = np.sqrt(aa)
    ab_2 = 2 * a * b
    ac_2 = 2 * a * c
    ad_2 = 2 * a * d
    bc_2 = 2 * b * c
    bd_2 = 2 * b * d
    cd_2 = 2 * c * d
    rotation = np.empty(quat.shape[:-1] + (3, 3))
    rotation[..., 0, 0] = aa + bb - cc - dd
    rotation[..., 0, 1] = bc_2 - ad_2
    rotation[..., 0, 2] = bd_2 + ac_2
    rotation[..., 1, 0] = bc_2 + ad_2
    rotation[..., 1, 1] = aa + cc - bb - dd
    rotation[..., 1, 2] = cd_2 - ab_2
    rotation[..., 2, 0] = bd_2 - ac_2
    rotation[..., 2, 1] = cd_2 + ab_2
    rotation[..., 2, 2] = aa + dd - bb - cc
    return rotation


@jit()
def _one_rot_to_quat(rot):
    """Convert a rotation matrix to quaternions."""
    # see e.g. http://www.euclideanspace.com/maths/geometry/rotations/
    #                 conversions/matrixToQuaternion/
    det = np.linalg.det(np.reshape(rot, (3, 3)))
    if np.abs(det - 1.0) > 1e-3:
        raise ValueError("Matrix is not a pure rotation, got determinant != 1")
    t = 1.0 + rot[0] + rot[4] + rot[8]
    if t > np.finfo(rot.dtype).eps:
        s = np.sqrt(t) * 2.0
        # qw = 0.25 * s
        qx = (rot[7] - rot[5]) / s
        qy = (rot[2] - rot[6]) / s
        qz = (rot[3] - rot[1]) / s
    elif rot[0] > rot[4] and rot[0] > rot[8]:
        s = np.sqrt(1.0 + rot[0] - rot[4] - rot[8]) * 2.0
        # qw = (rot[7] - rot[5]) / s
        qx = 0.25 * s
        qy = (rot[1] + rot[3]) / s
        qz = (rot[2] + rot[6]) / s
    elif rot[4] > rot[8]:
        s = np.sqrt(1.0 - rot[0] + rot[4] - rot[8]) * 2
        # qw = (rot[2] - rot[6]) / s
        qx = (rot[1] + rot[3]) / s
        qy = 0.25 * s
        qz = (rot[5] + rot[7]) / s
    else:
        s = np.sqrt(1.0 - rot[0] - rot[4] + rot[8]) * 2.0
        # qw = (rot[3] - rot[1]) / s
        qx = (rot[2] + rot[6]) / s
        qy = (rot[5] + rot[7]) / s
        qz = 0.25 * s
    return np.array((qx, qy, qz))


def rot_to_quat(rot):
    """Convert a set of rotations to quaternions.

    Parameters
    ----------
    rot : array, shape (..., 3, 3)
        The rotation matrices to convert.

    Returns
    -------
    quat : array, shape (..., 3)
        The q1, q2, and q3 (x, y, z) parameters of the corresponding
        unit quaternions.

    See Also
    --------
    quat_to_rot
    """
    rot = rot.reshape(rot.shape[:-2] + (9,))
    return np.apply_along_axis(_one_rot_to_quat, -1, rot)


def _quat_to_affine(quat):
    assert quat.shape == (6,)
    affine = np.eye(4)
    affine[:3, :3] = quat_to_rot(quat[:3])
    affine[:3, 3] = quat[3:]
    return affine


def _affine_to_quat(affine):
    assert affine.shape[-2:] == (4, 4)
    return np.concatenate(
        [rot_to_quat(affine[..., :3, :3]), affine[..., :3, 3]],
        axis=-1,
    )


def _angle_dist_between_rigid(a, b=None, *, angle_units="rad", distance_units="m"):
    a = _affine_to_quat(a)
    b = np.zeros(6) if b is None else _affine_to_quat(b)
    ang = _angle_between_quats(a[..., :3], b[..., :3])
    dist = np.linalg.norm(a[..., 3:] - b[..., 3:], axis=-1)
    assert isinstance(angle_units, str) and angle_units in ("rad", "deg")
    if angle_units == "deg":
        ang = np.rad2deg(ang)
    assert isinstance(distance_units, str) and distance_units in ("m", "mm")
    if distance_units == "mm":
        dist *= 1e3
    return ang, dist


def _angle_between_quats(x, y=None):
    """Compute the ang between two quaternions w/3-element representations."""
    # z = conj(x) * y
    # conjugate just negates all but the first element in a 4-element quat,
    # so it's just a negative for us
    y = np.zeros(3) if y is None else y
    z = _quat_mult(-x, y)
    z0 = _quat_real(z)
    return 2 * np.arctan2(np.linalg.norm(z, axis=-1), z0)


def _quat_real(quat):
    """Get the real part of our 3-element quat."""
    assert quat.shape[-1] == 3, quat.shape[-1]
    return np.sqrt(
        np.maximum(
            1.0
            - quat[..., 0] * quat[..., 0]
            - quat[..., 1] * quat[..., 1]
            - quat[..., 2] * quat[..., 2],
            0.0,
        )
    )


def _quat_mult(one, two):
    assert one.shape[-1] == two.shape[-1] == 3
    w1 = _quat_real(one)
    w2 = _quat_real(two)
    out = np.empty(np.broadcast(one, two).shape)
    # Most mathematical expressions use this sort of notation
    x1, x2 = one[..., 0], two[..., 0]
    y1, y2 = one[..., 1], two[..., 1]
    z1, z2 = one[..., 2], two[..., 2]
    out[..., 0] = w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2
    out[..., 1] = w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2
    out[..., 2] = w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2
    # only need to compute w because we need signs from it
    w = w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2
    signs = np.sign(w)
    signs = np.where(signs, signs, 1)
    out *= signs[..., np.newaxis]
    return out


def _skew_symmetric_cross(a):
    """Compute the skew-symmetric cross product of a vector."""
    return np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])


def _find_vector_rotation(a, b):
    """Find the rotation matrix that maps unit vector a to b."""
    # Rodrigues' rotation formula:
    #   https://en.wikipedia.org/wiki/Rodrigues%27_rotation_formula
    #   http://math.stackexchange.com/a/476311
    R = np.eye(3)
    v = np.cross(a, b)
    if np.allclose(v, 0.0):  # identical
        return R
    s = np.dot(v, v)  # sine of the angle between them
    c = np.dot(a, b)  # cosine of the angle between them
    vx = _skew_symmetric_cross(v)
    R += vx + np.dot(vx, vx) * (1 - c) / s
    return R


@jit()
def _fit_matched_points(p, x, weights=None, scale=False):
    """Fit matched points using an analytical formula."""
    # Follow notation of P.J. Besl and N.D. McKay, A Method for
    # Registration of 3-D Shapes, IEEE Trans. Patt. Anal. Machine Intell., 14,
    # 239 - 255, 1992.
    #
    # The original method is actually by Horn, Closed-form solution of absolute
    # orientation using unit quaternions, J Opt. Soc. Amer. A vol 4 no 4
    # pp 629-642, Apr. 1987. This paper describes how weights can be
    # easily incorporated, and a uniform scale factor can be computed.
    #
    # Caution: This can be dangerous if there are 3 points, or 4 points in
    #          a symmetric layout, as the geometry can be explained
    #          equivalently under 180 degree rotations.
    #
    # Eventually this can be extended to also handle a uniform scale factor,
    # as well.
    assert p.shape == x.shape
    assert p.ndim == 2
    assert p.shape[1] == 3
    # (weighted) centroids
    weights_ = np.full((p.shape[0], 1), 1.0 / max(p.shape[0], 1))
    if weights is not None:
        weights_[:] = np.reshape(weights / weights.sum(), (weights.size, 1))
    mu_p = np.dot(weights_.T, p)[0]
    mu_x = np.dot(weights_.T, x)[0]
    dots = np.dot(p.T, weights_ * x)
    Sigma_px = dots - np.outer(mu_p, mu_x)  # eq 24
    # x and p should no longer be used
    A_ij = Sigma_px - Sigma_px.T
    Delta = np.array([A_ij[1, 2], A_ij[2, 0], A_ij[0, 1]])
    tr_Sigma_px = np.trace(Sigma_px)
    # "N" in Horn:
    Q = np.empty((4, 4))
    Q[0, 0] = tr_Sigma_px
    Q[0, 1:] = Delta
    Q[1:, 0] = Delta
    Q[1:, 1:] = Sigma_px + Sigma_px.T - tr_Sigma_px * np.eye(3)
    _, v = np.linalg.eigh(Q)  # sorted ascending
    quat = np.empty(6)
    quat[:3] = v[1:, -1]
    if v[0, -1] != 0:
        quat[:3] *= np.sign(v[0, -1])
    rot = quat_to_rot(quat[:3])
    # scale factor is easy once we know the rotation
    if scale:  # p is "right" (from), x is "left" (to) in Horn 1987
        dev_x = x - mu_x
        dev_p = p - mu_p
        dev_x *= dev_x
        dev_p *= dev_p
        if weights is not None:
            dev_x *= weights_
            dev_p *= weights_
        s = np.sqrt(np.sum(dev_x) / np.sum(dev_p))
    else:
        s = 1.0
    # translation is easy once rotation and scale are known
    quat[3:] = mu_x - s * np.dot(rot, mu_p)
    return quat, s


def _average_quats(quats, weights=None):
    """Average unit quaternions properly."""
    assert quats.ndim == 2 and quats.shape[1] in (3, 4)
    if weights is None:
        weights = np.ones(quats.shape[0])
    assert (weights >= 0).all()
    norm = weights.sum()
    if weights.sum() == 0:
        return np.zeros(3)
    weights = weights / norm
    # The naive step here would be:
    #
    #     avg_quat = np.dot(weights, quats[:, :3])
    #
    # But this is not robust to quaternions having sign ambiguity,
    # i.e., q == -q. Thus we instead use the rank 1 update method:
    #
    #     https://arc.aiaa.org/doi/abs/10.2514/1.28949?journalCode=jgcd
    #     https://github.com/tolgabirdal/averaging_quaternions/blob/master/wavg_quaternion_markley.m  # noqa: E501
    #
    # We use unit quats and don't store the last element, so reconstruct it
    # to get our 4-element quaternions:
    quats = np.concatenate((_quat_real(quats)[..., np.newaxis], quats), -1)
    quats *= weights[:, np.newaxis]
    A = np.einsum("ij,ik->jk", quats, quats)  # sum of outer product of each q
    avg_quat = linalg.eigh(A)[1][:, -1]  # largest eigenvector is the avg
    # Same as the largest eigenvector from the concatenation of all as
    # svd(quats, full_matrices=False)[-1][0], but faster.
    #
    # By local convention we take the real term (which we remove from our
    # representation) as positive. Since it can be zero, let's just ensure
    # that the first non-zero element is positive. This shouldn't matter once
    # we go to a rotation matrix, but it's nice for testing to have
    # consistency.
    avg_quat *= np.sign(avg_quat[avg_quat != 0][0])
    avg_quat = avg_quat[1:]
    return avg_quat


@fill_doc
def read_ras_mni_t(subject, subjects_dir=None):
    """Read a subject's RAS to MNI transform.

    Parameters
    ----------
    subject : str
        The subject.
    %(subjects_dir)s

    Returns
    -------
    ras_mni_t : instance of Transform
        The transform from RAS to MNI (in mm).
    """
    subjects_dir = Path(get_subjects_dir(subjects_dir=subjects_dir, raise_error=True))
    _validate_type(subject, "str", "subject")
    fname = subjects_dir / subject / "mri" / "transforms" / "talairach.xfm"
    fname = str(
        _check_fname(
            fname,
            "read",
            True,
            "FreeSurfer Talairach transformation file",
        )
    )
    return Transform("ras", "mni_tal", _read_fs_xfm(fname)[0])


def _read_fs_xfm(fname):
    """Read a Freesurfer transform from a .xfm file."""
    assert fname.endswith(".xfm")
    with open(fname) as fid:
        logger.debug(f"Reading FreeSurfer talairach.xfm file:\n{fname}")

        # read lines until we get the string 'Linear_Transform', which precedes
        # the data transformation matrix
        comp = "Linear_Transform"
        for li, line in enumerate(fid):
            if li == 0:
                kind = line.strip()
                logger.debug(f"Found: {repr(kind)}")
            if line[: len(comp)] == comp:
                # we have the right line, so don't read any more
                break
        else:
            raise ValueError(
                f'Failed to find "Linear_Transform" string in xfm file:\n{fname}'
            )

        xfm = list()
        # read the transformation matrix (3x4)
        for ii, line in enumerate(fid):
            digs = [float(s) for s in line.strip("\n;").split()]
            xfm.append(digs)
            if ii == 2:
                break
        else:
            raise ValueError("Could not find enough linear transform lines")
    xfm.append([0.0, 0.0, 0.0, 1.0])
    xfm = np.array(xfm, dtype=float)
    return xfm, kind


def _write_fs_xfm(fname, xfm, kind):
    """Write a Freesurfer transform to a .xfm file."""
    with open(fname, "wb") as fid:
        fid.write((kind + "\n\nTtransform_Type = Linear;\n").encode("ascii"))
        fid.write("Linear_Transform =\n".encode("ascii"))
        for li, line in enumerate(xfm[:-1]):
            line = " ".join([f"{part:0.6f}" for part in line])
            line += "\n" if li < 2 else ";\n"
            fid.write(line.encode("ascii"))


def _quat_to_euler(quat):
    euler = np.empty(quat.shape)
    x, y, z = quat[..., 0], quat[..., 1], quat[..., 2]
    w = _quat_real(quat)
    np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y), out=euler[..., 0])
    np.arcsin(2 * (w * y - x * z), out=euler[..., 1])
    np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z), out=euler[..., 2])
    return euler


def _euler_to_quat(euler):
    quat = np.empty(euler.shape)
    phi, theta, psi = euler[..., 0] / 2, euler[..., 1] / 2, euler[..., 2] / 2
    cphi, sphi = np.cos(phi), np.sin(phi)
    del phi
    ctheta, stheta = np.cos(theta), np.sin(theta)
    del theta
    cpsi, spsi = np.cos(psi), np.sin(psi)
    del psi
    mult = np.sign(cphi * ctheta * cpsi + sphi * stheta * spsi)
    if np.isscalar(mult):
        mult = 1.0 if mult == 0 else mult
    else:
        mult[mult == 0] = 1.0
    mult = mult[..., np.newaxis]
    quat[..., 0] = sphi * ctheta * cpsi - cphi * stheta * spsi
    quat[..., 1] = cphi * stheta * cpsi + sphi * ctheta * spsi
    quat[..., 2] = cphi * ctheta * spsi - sphi * stheta * cpsi
    quat *= mult
    return quat


###############################################################################
# Affine Registration and SDR

_ORDERED_STEPS = ("translation", "rigid", "affine", "sdr")


def _validate_zooms(zooms):
    _validate_type(zooms, (dict, list, tuple, "numeric", None), "zooms")
    zooms = _handle_default("transform_zooms", zooms)
    for key, val in zooms.items():
        _check_option("zooms key", key, _ORDERED_STEPS)
        if val is not None:
            val = tuple(float(x) for x in np.array(val, dtype=float).ravel())
            _check_option(f"len(zooms[{repr(key)})", len(val), (1, 3))
            if len(val) == 1:
                val = val * 3
            for this_zoom in val:
                if this_zoom <= 1:
                    raise ValueError(f"Zooms must be > 1, got {this_zoom}")
            zooms[key] = val
    return zooms


def _validate_niter(niter):
    _validate_type(niter, (dict, list, tuple, None), "niter")
    niter = _handle_default("transform_niter", niter)
    for key, value in niter.items():
        _check_option("niter key", key, _ORDERED_STEPS)
        _check_option(f"len(niter[{repr(key)}])", len(value), (1, 2, 3))
    return niter


def _validate_pipeline(pipeline):
    _validate_type(pipeline, (str, list, tuple), "pipeline")
    pipeline_defaults = dict(
        all=_ORDERED_STEPS,
        rigids=_ORDERED_STEPS[: _ORDERED_STEPS.index("rigid") + 1],
        affines=_ORDERED_STEPS[: _ORDERED_STEPS.index("affine") + 1],
    )
    if isinstance(pipeline, str):  # use defaults
        _check_option(
            "pipeline", pipeline, ("all", "rigids", "affines"), extra="when str"
        )
        pipeline = pipeline_defaults[pipeline]
    for ii, step in enumerate(pipeline):
        name = f"pipeline[{ii}]"
        _validate_type(step, str, name)
        _check_option(name, step, _ORDERED_STEPS)
    ordered_pipeline = tuple(sorted(pipeline, key=lambda x: _ORDERED_STEPS.index(x)))
    if tuple(pipeline) != ordered_pipeline:
        raise ValueError(
            f"Steps in pipeline are out of order, expected {ordered_pipeline} "
            f"but got {pipeline} instead"
        )
    if len(set(pipeline)) != len(pipeline):
        raise ValueError("Steps in pipeline should not be repeated")
    return tuple(pipeline)


def _compute_r2(a, b):
    return 100 * (a.ravel() @ b.ravel()) / (np.linalg.norm(a) * np.linalg.norm(b))


def _reslice_normalize(img, zooms):
    from dipy.align.reslice import reslice

    img_zooms = img.header.get_zooms()[:3]
    img_affine = img.affine
    img = _get_img_fdata(img)
    if zooms is not None:
        img, img_affine = reslice(img, img_affine, img_zooms, zooms)
    img /= img.max()  # normalize
    return img, img_affine


@verbose
def compute_volume_registration(
    moving,
    static,
    pipeline="all",
    zooms=None,
    niter=None,
    *,
    starting_affine=None,
    verbose=None,
):
    """Align two volumes using an affine and, optionally, SDR.

    Parameters
    ----------
    %(moving)s
    %(static)s
    %(pipeline)s
    zooms : float | tuple | dict | None
        The voxel size of volume for each spatial dimension in mm.
        If None (default), MRIs won't be resliced (slow, but most accurate).
        Can be a tuple to provide separate zooms for each dimension (X/Y/Z),
        or a dict with keys ``['translation', 'rigid', 'affine', 'sdr']``
        (each with values that are float`, tuple, or None) to provide separate
        reslicing/accuracy for the steps.
    %(niter)s
    starting_affine : ndarray
        The affine to initialize the registration with.

        .. versionadded:: 1.2
    %(verbose)s

    Returns
    -------
    %(reg_affine)s
    %(sdr_morph)s

    Notes
    -----
    This function is heavily inspired by and extends
    :func:`dipy.align.affine_registration
    <dipy.align._public.affine_registration>`.

    .. versionadded:: 0.24
    """
    return _compute_volume_registration(
        moving, static, pipeline, zooms, niter, starting_affine=starting_affine
    )[:2]


def _compute_volume_registration(
    moving, static, pipeline, zooms, niter, *, starting_affine=None
):
    nib = _import_nibabel("SDR morph")
    _require_version("dipy", "SDR morph", "0.10.1")
    with np.testing.suppress_warnings():
        from dipy.align import (
            affine,
            affine_registration,
            center_of_mass,
            imwarp,
            metrics,
            rigid,
            translation,
        )
        from dipy.align.imaffine import AffineMap

    # input validation
    _validate_type(moving, nib.spatialimages.SpatialImage, "moving")
    _validate_type(static, nib.spatialimages.SpatialImage, "static")
    original_zoom = np.mean(moving.header.get_zooms()[:3])
    zooms = _validate_zooms(zooms)
    niter = _validate_niter(niter)
    pipeline = _validate_pipeline(pipeline)

    logger.info("Computing registration...")

    # affine optimizations
    reg_affine = starting_affine
    sdr_morph = None
    pipeline_options = dict(
        translation=[center_of_mass, translation], rigid=[rigid], affine=[affine]
    )
    sigmas_mm = np.array([3.0, 1.0, 0.0])  # default for affine_registration
    sigma_diff_mm = 2.0
    factors = [4, 2, 1]
    current_zoom = None
    for i, step in enumerate(pipeline):
        # reslice image with zooms
        if i == 0 or zooms[step] != zooms[pipeline[i - 1]]:
            if zooms[step] is not None:
                logger.info(f"Reslicing to zooms={zooms[step]} for {step} ...")
                current_zoom = np.mean(zooms[step])
            else:
                logger.info(f"Using original zooms for {step} ...")
                current_zoom = original_zoom
            static_zoomed, static_affine = _reslice_normalize(static, zooms[step])
            moving_zoomed, moving_affine = _reslice_normalize(moving, zooms[step])
        logger.info(f"Optimizing {step}:")
        if step == "sdr":  # happens last
            sigma_diff_vox = sigma_diff_mm / current_zoom
            affine_map = AffineMap(
                reg_affine,  # apply registration here
                domain_grid_shape=static_zoomed.shape,
                domain_grid2world=static_affine,
                codomain_grid_shape=moving_zoomed.shape,
                codomain_grid2world=moving_affine,
            )
            moving_zoomed = affine_map.transform(moving_zoomed)
            metric = metrics.CCMetric(
                dim=3,
                sigma_diff=sigma_diff_vox,
                radius=max(int(np.ceil(2 * sigma_diff_vox)), 1),
            )
            sdr = imwarp.SymmetricDiffeomorphicRegistration(
                metric,
                level_iters=niter[step],
            )
            with wrapped_stdout(indent="    ", cull_newlines=True):
                sdr_morph = sdr.optimize(
                    static_zoomed,
                    moving_zoomed,
                    static_grid2world=static_affine,
                    moving_grid2world=static_affine,
                )
            moved_zoomed = sdr_morph.transform(moving_zoomed)
        else:
            sigmas_vox = list(sigmas_mm / current_zoom)
            with wrapped_stdout(indent="    ", cull_newlines=True):
                moved_zoomed, reg_affine = affine_registration(
                    moving_zoomed,
                    static_zoomed,
                    moving_affine=moving_affine,
                    static_affine=static_affine,
                    nbins=32,
                    metric="MI",
                    pipeline=pipeline_options[step],
                    level_iters=niter[step],
                    sigmas=sigmas_vox,
                    factors=factors,
                    starting_affine=reg_affine,
                )

            # report some useful information
            if step in ("translation", "rigid"):
                angle, dist = _angle_dist_between_rigid(reg_affine, angle_units="deg")
                logger.info(f"    Translation: {dist:6.1f} mm")
                if step == "rigid":
                    logger.info(f"    Rotation:    {angle:6.1f}°")
        assert moved_zoomed.shape == static_zoomed.shape, step
        r2 = _compute_r2(static_zoomed, moved_zoomed)
        logger.info(f"    R²:          {r2:6.1f}%")
    return (
        reg_affine,
        sdr_morph,
        static_zoomed.shape,
        static_affine,
        moving_zoomed.shape,
        moving_affine,
    )


@verbose
def apply_volume_registration(
    moving,
    static,
    reg_affine,
    sdr_morph=None,
    interpolation="linear",
    cval=0.0,
    verbose=None,
):
    """Apply volume registration.

    Uses registration parameters computed by
    :func:`~mne.transforms.compute_volume_registration`.

    Parameters
    ----------
    %(moving)s
    %(static)s
    %(reg_affine)s
    %(sdr_morph)s
    interpolation : str
        Interpolation to be used during the interpolation.
        Can be ``"linear"`` (default) or ``"nearest"``.
    cval : float | str
        The constant value to assume exists outside the bounds of the
        ``moving`` image domain. Can be a string percentage like ``'1%%'``
        to use the given percentile of image data as the constant value.
    %(verbose)s

    Returns
    -------
    reg_img : instance of SpatialImage
        The image after affine (and SDR, if provided) registration.

    Notes
    -----
    .. versionadded:: 0.24
    """
    _require_version("dipy", "SDR morph", "0.10.1")
    _import_nibabel("SDR morph")
    from dipy.align.imaffine import AffineMap
    from dipy.align.imwarp import DiffeomorphicMap
    from nibabel.spatialimages import SpatialImage

    _validate_type(moving, SpatialImage, "moving")
    _validate_type(static, SpatialImage, "static")
    _validate_type(reg_affine, np.ndarray, "reg_affine")
    _check_option("reg_affine.shape", reg_affine.shape, ((4, 4),))
    _validate_type(sdr_morph, (DiffeomorphicMap, None), "sdr_morph")
    _validate_type(cval, ("numeric", str), "cval")
    perc = None
    if isinstance(cval, str):
        if not cval.endswith("%"):
            raise ValueError(f"cval must end with % if str, got {cval}")
        perc = float(cval[:-1])
    logger.info("Applying affine registration ...")
    moving_affine = moving.affine
    moving = np.asarray(moving.dataobj, dtype=float)
    if perc is not None:
        cval = np.percentile(moving, perc)
        logger.info(f"Using a lower bound at the {perc} percentile: {cval}")
    moving -= cval
    static, static_affine = np.asarray(static.dataobj), static.affine
    affine_map = AffineMap(
        reg_affine,
        domain_grid_shape=static.shape,
        domain_grid2world=static_affine,
        codomain_grid_shape=moving.shape,
        codomain_grid2world=moving_affine,
    )
    reg_data = affine_map.transform(moving, interpolation=interpolation)
    if sdr_morph is not None:
        logger.info("Applying SDR warp ...")
        reg_data = sdr_morph.transform(
            reg_data,
            interpolation=interpolation,
            image_world2grid=np.linalg.inv(static_affine),
            out_shape=static.shape,
            out_grid2world=static_affine,
        )
    reg_data += cval
    reg_img = SpatialImage(reg_data, static_affine)
    logger.info("[done]")
    return reg_img


@verbose
def apply_volume_registration_points(
    info, trans, moving, static, reg_affine, sdr_morph=None, verbose=None
):
    """Apply volume registration.

    Uses registration parameters computed by
    :func:`~mne.transforms.compute_volume_registration`.

    Parameters
    ----------
    %(info_not_none)s
    %(trans_not_none)s
    %(moving)s
    %(static)s
    %(reg_affine)s
    %(sdr_morph)s
    %(verbose)s

    Returns
    -------
    %(info_not_none)s
    trans2 : instance of Transform
        The head->mri (surface RAS) transform for the static image.

    Notes
    -----
    .. versionadded:: 1.4.0
    """
    from .channels import compute_native_head_t, make_dig_montage

    _require_version("nibabel", "volume registration", "2.1.0")
    from dipy.align.imwarp import DiffeomorphicMap
    from nibabel import MGHImage
    from nibabel.spatialimages import SpatialImage

    _validate_type(moving, SpatialImage, "moving")
    _validate_type(static, SpatialImage, "static")
    _validate_type(reg_affine, np.ndarray, "reg_affine")
    _check_option("reg_affine.shape", reg_affine.shape, ((4, 4),))
    _validate_type(sdr_morph, (DiffeomorphicMap, None), "sdr_morph")

    moving_mgh = MGHImage(np.array(moving.dataobj).astype(np.float32), moving.affine)
    static_mgh = MGHImage(np.array(static.dataobj).astype(np.float32), static.affine)

    montage = info.get_montage()
    montage_kwargs = montage.get_positions()
    trans = _ensure_trans(trans, "head", "mri")
    montage.apply_trans(trans)  # to moving surface RAS

    locs = np.array(list(montage.get_positions()["ch_pos"].values()))

    locs = apply_trans(
        Transform(  # to moving voxels
            fro="mri",
            to="mri_voxel",
            trans=np.linalg.inv(moving_mgh.header.get_vox2ras_tkr()),
        ),
        locs * 1000,
    )
    locs = apply_trans(
        Transform(  # to moving ras
            fro="mri_voxel", to="ras", trans=moving_mgh.header.get_vox2ras()
        ),
        locs,
    )
    locs = apply_trans(
        Transform(  # to static ras
            fro="ras", to="ras", trans=np.linalg.inv(reg_affine)
        ),
        locs,
    )
    if sdr_morph is not None:
        _require_version("dipy", "SDR morph", "1.6.0")
        locs = sdr_morph.transform_points(
            locs,
            coord2world=sdr_morph.domain_grid2world,
            world2coord=sdr_morph.domain_world2grid,
        )
    locs = apply_trans(
        Transform(  # to static voxels
            fro="ras",
            to="mri_voxel",
            trans=np.linalg.inv(static_mgh.header.get_vox2ras()),
        ),
        locs,
    )
    locs = (
        apply_trans(
            Transform(  # to static surface RAS
                fro="mri_voxel", to="mri", trans=static_mgh.header.get_vox2ras_tkr()
            ),
            locs,
        )
        / 1000
    )

    montage_kwargs["coord_frame"] = "mri"
    montage_kwargs["ch_pos"] = {ch: loc for ch, loc in zip(montage.ch_names, locs)}
    montage2 = make_dig_montage(**montage_kwargs)

    trans2 = compute_native_head_t(montage2)
    info2 = info.copy()
    info2.set_montage(montage2)  # converts to head coordinates

    return info2, trans2


class _MatchedDisplacementFieldInterpolator:
    """Interpolate from matched points using a displacement field in ND.

    For a demo, see
    https://gist.github.com/larsoner/fbe32d57996848395854d5e59dff1e10
    and related tests.
    """

    def __init__(self, fro, to, *, extrema=None):
        from scipy.interpolate import LinearNDInterpolator

        fro = np.array(fro, float)
        to = np.array(to, float)
        assert fro.shape == to.shape
        assert fro.ndim == 2
        # this restriction is only necessary because it's what
        # _fit_matched_points requires
        assert fro.shape[1] == 3

        # Prealign using affine + uniform scaling
        self._quat, self._scale = _fit_matched_points(fro, to, scale=True)
        trans = _quat_to_affine(self._quat)
        trans[:3, :3] *= self._scale
        self._affine = trans
        fro = apply_trans(trans, fro)

        # Add points at extrema
        if extrema is None:
            delta = (to.max(axis=0) - to.min(axis=0)) / 2.0
            assert (delta > 0).all()
            extrema = np.array([fro.min(axis=0) - delta, fro.max(axis=0) + delta])
        assert extrema.shape == (2, 3)  # min, max
        self._extrema = np.array(np.meshgrid(*extrema.T)).T.reshape(-1, fro.shape[-1])
        fro_concat = np.concatenate((fro, self._extrema))
        to_concat = np.concatenate((to, self._extrema))

        # Compute the interpolator (which internally uses Delaunay)
        self._interp = LinearNDInterpolator(fro_concat, to_concat)

    def __call__(self, x):
        assert x.ndim in (1, 2) and x.shape[-1] == 3
        assert np.isfinite(x).all()
        singleton = x.ndim == 1
        x = apply_trans(self._affine, x)
        assert np.isfinite(x).all()
        out = self._interp(x)
        assert np.isfinite(out).all()
        self._last_deltas = np.linalg.norm(x - out, axis=1)
        out = out[0] if singleton else out
        return out
