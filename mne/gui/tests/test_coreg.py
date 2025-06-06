# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import os
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_allclose

import mne
from mne._fiff.constants import FIFF
from mne.channels import DigMontage
from mne.coreg import Coregistration
from mne.datasets import testing
from mne.io import read_info
from mne.utils import catch_logging, get_config
from mne.viz import _3d

data_path = testing.data_path(download=False)
raw_path = data_path / "MEG" / "sample" / "sample_audvis_trunc_raw.fif"
fname_trans = data_path / "MEG" / "sample" / "sample_audvis_trunc-trans.fif"
subjects_dir = data_path / "subjects"
fid_fname = subjects_dir / "sample" / "bem" / "sample-fiducials.fif"
ctf_raw_path = data_path / "CTF" / "catch-alp-good-f.ds"
nirx_15_0_raw_path = (
    data_path / "NIRx" / "nirscout" / "nirx_15_0_recording" / "NIRS-2019-10-27_003.hdr"
)
nirsport2_raw_path = (
    data_path / "NIRx" / "nirsport_v2" / "aurora_2021_9" / "2021-10-01_002_config.hdr"
)
snirf_nirsport2_raw_path = (
    data_path / "SNIRF" / "NIRx" / "NIRSport2" / "1.0.3" / "2021-05-05_001.snirf"
)


pytest.importorskip("nibabel")


class TstVTKPicker:
    """Class to test cell picking."""

    def __init__(self, mesh, cell_id, event_pos):
        self.mesh = mesh
        self.cell_id = cell_id
        self.point_id = None
        self.event_pos = event_pos

    def GetCellId(self):
        """Return the picked cell."""
        return self.cell_id

    def GetDataSet(self):
        """Return the picked mesh."""
        return self.mesh

    def GetPickPosition(self):
        """Return the picked position."""
        vtk_cell = self.mesh.GetCell(self.cell_id)
        cell = [
            vtk_cell.GetPointId(point_id)
            for point_id in range(vtk_cell.GetNumberOfPoints())
        ]
        self.point_id = cell[0]
        return self.mesh.points[self.point_id]

    def GetEventPosition(self):
        """Return event position."""
        return self.event_pos


@pytest.mark.slowtest
@testing.requires_testing_data
@pytest.mark.parametrize(
    "inst_path",
    (
        raw_path,
        "gen_montage",
        ctf_raw_path,
        nirx_15_0_raw_path,
        nirsport2_raw_path,
        snirf_nirsport2_raw_path,
    ),
)
def test_coreg_gui_pyvista_file_support(
    inst_path, tmp_path, renderer_interactive_pyvistaqt
):
    """Test reading supported files."""
    from mne.gui import coregistration

    if inst_path == "gen_montage":
        # generate a montage fig to use as inst.
        tmp_info = read_info(raw_path)
        eeg_chans = []
        for pt in tmp_info["dig"]:
            if pt["kind"] == FIFF.FIFFV_POINT_EEG:
                eeg_chans.append(f"EEG {pt['ident']:03d}")

        dig = DigMontage(dig=tmp_info["dig"], ch_names=eeg_chans)
        inst_path = tmp_path / "tmp-dig.fif"
        dig.save(inst_path)

    if inst_path == ctf_raw_path:
        ctx = pytest.warns(RuntimeWarning, match="MEG ref channel RMSP")
    elif inst_path == snirf_nirsport2_raw_path:  # TODO: This is maybe a bug?
        ctx = pytest.warns(RuntimeWarning, match='assuming "head"')
    else:
        ctx = nullcontext()
    with ctx:
        coreg = coregistration(
            inst=inst_path, subject="sample", subjects_dir=subjects_dir
        )
    coreg._accept_close_event = True
    coreg.close()


@pytest.mark.slowtest
@testing.requires_testing_data
def test_coreg_gui_pyvista_basic(tmp_path, monkeypatch, renderer_interactive_pyvistaqt):
    """Test that using CoregistrationUI matches mne coreg."""
    from mne.gui import coregistration

    config = get_config()
    # the sample subject in testing has MRI fids
    assert (subjects_dir / "sample" / "bem" / "sample-fiducials.fif").is_file()

    coreg = coregistration(
        subject="sample", subjects_dir=subjects_dir, trans=fname_trans
    )
    assert coreg._lock_fids
    coreg._reset_fiducials()
    coreg.close()

    # make it always log the distances
    monkeypatch.setattr(_3d.logger, "info", _3d.logger.warning)
    with catch_logging() as log:
        coreg = coregistration(
            inst=raw_path,
            subject="sample",
            head_high_res=False,  # for speed
            subjects_dir=subjects_dir,
            verbose="debug",
        )
    log = log.getvalue()
    assert "Total 16/78 points inside the surface" in log
    coreg._set_fiducials_file(fid_fname)
    assert coreg._fiducials_file == str(fid_fname)

    # fitting (with scaling)
    assert not coreg._mri_scale_modified
    coreg._reset()
    coreg._reset_fitting_parameters()
    coreg._set_scale_mode("uniform")
    coreg._fits_fiducials()
    assert_allclose(
        coreg.coreg._scale, np.array([97.46, 97.46, 97.46]) * 1e-2, atol=1e-3
    )
    shown_scale = [coreg._widgets[f"s{x}"].get_value() for x in "XYZ"]
    assert_allclose(shown_scale, coreg.coreg._scale * 100, atol=1e-2)
    coreg._set_icp_fid_match("nearest")
    coreg._set_scale_mode("3-axis")
    coreg._fits_icp()
    assert_allclose(
        coreg.coreg._scale, np.array([104.43, 101.47, 125.78]) * 1e-2, atol=1e-3
    )
    shown_scale = [coreg._widgets[f"s{x}"].get_value() for x in "XYZ"]
    assert_allclose(shown_scale, coreg.coreg._scale * 100, atol=1e-2)
    coreg._set_scale_mode("None")
    coreg._set_icp_fid_match("matched")
    assert coreg._mri_scale_modified

    # unlock fiducials
    assert coreg._lock_fids
    coreg._set_lock_fids(False)
    assert not coreg._lock_fids

    # picking
    assert not coreg._mri_fids_modified
    vtk_picker = TstVTKPicker(coreg._surfaces["head"], 0, (0, 0))
    coreg._on_mouse_move(vtk_picker, None)
    coreg._on_button_press(vtk_picker, None)
    coreg._on_pick(vtk_picker, None)
    coreg._on_button_release(vtk_picker, None)
    coreg._on_pick(vtk_picker, None)  # also pick when locked
    assert coreg._mri_fids_modified

    # lock fiducials
    coreg._set_lock_fids(True)
    assert coreg._lock_fids

    # fitting (no scaling)
    assert coreg._nasion_weight == 10.0
    coreg._set_point_weight(11.0, "nasion")
    assert coreg._nasion_weight == 11.0
    coreg._fit_fiducials()
    with catch_logging() as log:
        coreg._redraw()  # actually emit the log
    log = log.getvalue()
    assert "Total 6/78 points inside the surface" in log
    with catch_logging() as log:
        coreg._fit_icp()
        coreg._redraw()
    log = log.getvalue()
    assert "Total 38/78 points inside the surface" in log
    assert coreg.coreg._extra_points_filter is None
    coreg._omit_hsp()
    with catch_logging() as log:
        coreg._redraw()
    log = log.getvalue()
    assert "Total 29/53 points inside the surface" in log
    assert coreg.coreg._extra_points_filter is not None
    coreg._reset_omit_hsp_filter()
    with catch_logging() as log:
        coreg._redraw()
    log = log.getvalue()
    assert "Total 38/78 points inside the surface" in log
    assert coreg.coreg._extra_points_filter is None

    assert coreg._grow_hair == 0
    coreg._fit_fiducials()  # go back to few inside to start
    with catch_logging() as log:
        coreg._redraw()
    log = log.getvalue()
    assert "Total 6/78 points inside the surface" in log
    norm = np.linalg.norm(coreg._head_geo["rr"])  # what's used for inside
    assert_allclose(norm, 5.949288, atol=1e-3)
    coreg._set_grow_hair(20.0)
    with catch_logging() as log:
        coreg._redraw()
    assert coreg._grow_hair == 20.0
    norm = np.linalg.norm(coreg._head_geo["rr"])
    assert_allclose(norm, 6.555220, atol=1e-3)  # outward
    log = log.getvalue()
    assert "Total 8/78 points inside the surface" in log  # more outside now

    # visualization
    assert not coreg._helmet
    assert coreg._actors["helmet"] is None
    coreg._set_helmet(True)
    assert coreg._eeg_channels
    coreg._set_eeg_channels(False)
    assert not coreg._eeg_channels
    assert coreg._helmet
    with catch_logging() as log:
        coreg._redraw(verbose="debug")
    log = log.getvalue()
    assert "Drawing helmet" in log
    coreg._set_point_weight(1.0, "nasion")
    coreg._fit_fiducials()
    with catch_logging() as log:
        coreg._redraw(verbose="debug")
    log = log.getvalue()
    assert "Drawing helmet" in log
    assert not coreg._meg_channels
    assert coreg._actors["helmet"] is not None
    # TODO: Someday test our file dialogs like:
    # coreg._widgets["save_trans"].widget.click()
    assert len(coreg._actors["sensors"]) == 0
    coreg._set_meg_channels(True)
    assert coreg._meg_channels
    with catch_logging() as log:
        coreg._redraw(verbose="debug")
    assert "Drawing meg sensors" in log.getvalue()
    assert coreg._actors["helmet"] is not None
    assert len(coreg._actors["sensors"]) == 306
    assert coreg._orient_glyphs
    assert coreg._scale_by_distance
    assert coreg._mark_inside
    assert_allclose(
        coreg._head_opacity, float(config.get("MNE_COREG_HEAD_OPACITY", "0.8"))
    )
    assert coreg._hpi_coils
    assert coreg._head_shape_points
    assert coreg._scale_mode == "None"
    assert coreg._icp_fid_match == "matched"
    assert coreg._head_resolution is False

    assert coreg._trans_modified
    tmp_trans = tmp_path / "tmp-trans.fif"
    coreg._save_trans(tmp_trans)
    assert not coreg._trans_modified
    assert tmp_trans.is_file()

    # first, disable auto cleanup
    coreg._renderer._window_close_disconnect(after=True)
    # test _close_callback()
    coreg._renderer._process_events()
    assert coreg._mri_fids_modified  # should prompt
    assert coreg._renderer.plotter.app_window.children() is not None
    assert "close_dialog" not in coreg._widgets
    assert not coreg._renderer.plotter._closed
    assert coreg._accept_close_event
    # make sure it's ignored (PySide6 causes problems here and doesn't wait)
    coreg._accept_close_event = False
    coreg.close()
    assert not coreg._renderer.plotter._closed
    coreg._widgets["close_dialog"].trigger("Discard")  # do not save
    coreg.close()
    assert coreg._renderer.plotter._closed
    coreg._clean()  # finally, cleanup internal structures
    assert coreg._renderer is None

    # Coregistration instance should survive
    assert isinstance(coreg.coreg, Coregistration)


@pytest.mark.slowtest
@testing.requires_testing_data
def test_fullscreen(renderer_interactive_pyvistaqt):
    """Test fullscreen mode."""
    from mne.gui import coregistration

    # Fullscreen mode
    coreg = coregistration(subject="sample", subjects_dir=subjects_dir, fullscreen=True)
    coreg._accept_close_event = True
    coreg.close()


@pytest.mark.slowtest
@testing.requires_testing_data
def test_coreg_gui_scraper(tmp_path, renderer_interactive_pyvistaqt):
    """Test the scrapper for the coregistration GUI."""
    pytest.importorskip("sphinx_gallery")
    from mne.gui import coregistration

    coreg = coregistration(
        subject="sample", subjects_dir=subjects_dir, trans=fname_trans
    )
    (tmp_path / "_images").mkdir()
    image_path = tmp_path / "_images" / "temp.png"
    gallery_conf = dict(builder_name="html", src_dir=tmp_path)
    block_vars = dict(
        example_globals=dict(gui=coreg), image_path_iterator=iter([str(image_path)])
    )
    assert not image_path.is_file()
    assert not getattr(coreg, "_scraped", False)
    mne.gui._GUIScraper()(None, block_vars, gallery_conf)
    assert image_path.is_file()
    assert coreg._scraped


@pytest.mark.slowtest
@testing.requires_testing_data
def test_coreg_gui_notebook(renderer_notebook, nbexec):
    """Test the coregistration UI in a notebook."""
    import pytest

    import mne
    from mne.datasets import testing
    from mne.gui import coregistration

    mne.viz.set_3d_backend("notebook")  # set the 3d backend
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("_MNE_FAKE_HOME_DIR")
        data_path = testing.data_path(download=False)
    subjects_dir = data_path / "subjects"
    coregistration(subject="sample", subjects_dir=subjects_dir)


@pytest.mark.slowtest
def test_no_sparse_head(subjects_dir_tmp, renderer_interactive_pyvistaqt, monkeypatch):
    """Test mne.gui.coregistration with no sparse head."""
    from mne.gui import coregistration

    subjects_dir_tmp = Path(subjects_dir_tmp)
    subject = "sample"
    out_rr, out_tris = mne.read_surface(
        subjects_dir_tmp / subject / "bem" / "outer_skin.surf"
    )
    for head in ("sample-head.fif", "outer_skin.surf"):
        os.remove(subjects_dir_tmp / subject / "bem" / head)
    # Avoid actually doing the decimation (it's slow)
    monkeypatch.setattr(
        mne.coreg, "decimate_surface", lambda rr, tris, n_triangles: (out_rr, out_tris)
    )
    with pytest.warns(RuntimeWarning, match="No low-resolution head found"):
        coreg = coregistration(
            inst=raw_path, subject=subject, subjects_dir=subjects_dir_tmp
        )
    coreg.close()


def test_splash_closed(tmp_path, renderer_interactive_pyvistaqt):
    """Test that the splash closes on error."""
    from mne.gui import coregistration

    with pytest.raises(RuntimeError, match="No standard head model"):
        coregistration(subjects_dir=tmp_path, subject="fsaverage")
