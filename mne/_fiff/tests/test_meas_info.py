# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import pickle
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal
from scipy import sparse

from mne import (
    Annotations,
    Epochs,
    compute_covariance,
    make_forward_solution,
    make_sphere_model,
    pick_info,
    pick_types,
    read_cov,
    read_epochs,
    read_events,
    read_evokeds,
    read_forward_solution,
    setup_volume_source_space,
    write_cov,
    write_forward_solution,
)
from mne._fiff import meas_info, tag
from mne._fiff._digitization import DigPoint, _make_dig_points
from mne._fiff.constants import FIFF
from mne._fiff.meas_info import (
    RAW_INFO_FIELDS,
    Info,
    MNEBadsList,
    _add_timedelta_to_stamp,
    _bad_chans_comp,
    _dt_to_stamp,
    _force_update_info,
    _get_valid_units,
    _merge_info,
    _read_extended_ch_info,
    _stamp_to_dt,
    anonymize_info,
    create_info,
    read_fiducials,
    read_info,
    write_fiducials,
    write_info,
)
from mne._fiff.proj import Projection
from mne._fiff.tag import _coil_trans_to_loc, _loc_to_coil_trans
from mne._fiff.write import DATE_NONE, _generate_meas_id
from mne.channels import (
    equalize_channels,
    make_standard_montage,
    read_polhemus_fastscan,
)
from mne.datasets import testing
from mne.event import make_fixed_length_events
from mne.io import BaseRaw, RawArray, read_raw_ctf, read_raw_fif
from mne.minimum_norm import (
    apply_inverse,
    make_inverse_operator,
    read_inverse_operator,
    write_inverse_operator,
)
from mne.transforms import Transform
from mne.utils import (
    _empty_hash,
    _record_warnings,
    assert_object_equal,
    catch_logging,
    object_diff,
)

root_dir = Path(__file__).parents[2]
fiducials_fname = root_dir / "data" / "fsaverage" / "fsaverage-fiducials.fif"
base_dir = root_dir / "io" / "tests" / "data"
raw_fname = base_dir / "test_raw.fif"
chpi_fname = base_dir / "test_chpi_raw_sss.fif"
event_name = base_dir / "test-eve.fif"

kit_data_dir = root_dir / "io" / "kit" / "tests" / "data"
hsp_fname = kit_data_dir / "test_hsp.txt"
elp_fname = kit_data_dir / "test_elp.txt"

data_path = testing.data_path(download=False)
sss_path = data_path / "SSS"
sss_ctc_fname = sss_path / "test_move_anon_crossTalk_raw_sss.fif"
ctf_fname = data_path / "CTF" / "testdata_ctf.ds"
raw_invalid_bday_fname = data_path / "misc" / "sample_invalid_birthday_raw.fif"


@pytest.mark.parametrize(
    "kwargs, want",
    [
        (dict(meg=False, eeg=True), [0]),
        (dict(meg=False, fnirs=True), [5]),
        (dict(meg=False, fnirs="hbo"), [5]),
        (dict(meg=False, fnirs="hbr"), []),
        (dict(meg=False, misc=True), [1]),
        (dict(meg=True), [2, 3, 4]),
        (dict(meg="grad"), [2, 3]),
        (dict(meg="planar1"), [2]),
        (dict(meg="planar2"), [3]),
        (dict(meg="mag"), [4]),
    ],
)
def test_create_info_grad(kwargs, want):
    """Test create_info behavior with grad coils."""
    info = create_info(6, 256, ["eeg", "misc", "grad", "grad", "mag", "hbo"])
    # Put these in an order such that grads get named "2" and "3", since
    # they get picked based first on coil_type then ch_name...
    assert [
        ch["ch_name"]
        for ch in info["chs"]
        if ch["coil_type"] == FIFF.FIFFV_COIL_VV_PLANAR_T1
    ] == ["2", "3"]
    picks = pick_types(info, **kwargs)
    assert_array_equal(picks, want)


def test_get_valid_units():
    """Test the valid units."""
    valid_units = _get_valid_units()
    assert isinstance(valid_units, tuple)
    assert all(isinstance(unit, str) for unit in valid_units)
    assert "n/a" in valid_units


def test_coil_trans():
    """Test loc<->coil_trans functions."""
    rng = np.random.RandomState(0)
    x = rng.randn(4, 4)
    x[3] = [0, 0, 0, 1]
    assert_allclose(_loc_to_coil_trans(_coil_trans_to_loc(x)), x)
    x = rng.randn(12)
    assert_allclose(_coil_trans_to_loc(_loc_to_coil_trans(x)), x)


def test_make_info():
    """Test some create_info properties."""
    n_ch = np.longlong(1)
    info = create_info(n_ch, 1000.0, "eeg")
    assert set(info.keys()) == set(RAW_INFO_FIELDS)

    coil_types = {ch["coil_type"] for ch in info["chs"]}
    assert FIFF.FIFFV_COIL_EEG in coil_types

    pytest.raises(TypeError, create_info, ch_names="Test Ch", sfreq=1000)
    pytest.raises(ValueError, create_info, ch_names=["Test Ch"], sfreq=-1000)
    pytest.raises(
        ValueError,
        create_info,
        ch_names=["Test Ch"],
        sfreq=1000,
        ch_types=["eeg", "eeg"],
    )
    pytest.raises(TypeError, create_info, ch_names=[np.array([1])], sfreq=1000)
    pytest.raises(
        KeyError, create_info, ch_names=["Test Ch"], sfreq=1000, ch_types=np.array([1])
    )
    pytest.raises(
        KeyError, create_info, ch_names=["Test Ch"], sfreq=1000, ch_types="awesome"
    )
    pytest.raises(
        TypeError, create_info, ["Test Ch"], sfreq=1000, montage=np.array([1])
    )
    m = make_standard_montage("biosemi32")
    info = create_info(ch_names=m.ch_names, sfreq=1000.0, ch_types="eeg")
    info.set_montage(m)
    ch_pos = [ch["loc"][:3] for ch in info["chs"]]
    ch_pos_mon = m._get_ch_pos()
    ch_pos_mon = np.array([ch_pos_mon[ch_name] for ch_name in info["ch_names"]])
    # transform to head
    ch_pos_mon += (0.0, 0.0, 0.04014)
    assert_allclose(ch_pos, ch_pos_mon, atol=1e-5)


def test_duplicate_name_correction():
    """Test duplicate channel names with running number."""
    # When running number is possible
    info = create_info(["A", "A", "A"], 1000.0, verbose="error")
    assert info["ch_names"] == ["A-0", "A-1", "A-2"]

    # When running number is not possible but alpha numeric is
    info = create_info(["A", "A", "A-0"], 1000.0, verbose="error")
    assert info["ch_names"] == ["A-a", "A-1", "A-0"]

    # When a single addition is not sufficient
    with pytest.raises(ValueError, match="Adding a single alphanumeric"):
        ch_n = ["A", "A"]
        # add all options for first duplicate channel (0)
        ch_n.extend([f"{ch_n[0]}-{c}" for c in string.ascii_lowercase + "0"])
        create_info(ch_n, 1000.0, verbose="error")


def test_fiducials_io(tmp_path):
    """Test fiducials i/o."""
    pts, coord_frame = read_fiducials(fiducials_fname)
    assert pts[0]["coord_frame"] == FIFF.FIFFV_COORD_MRI
    assert pts[0]["ident"] == FIFF.FIFFV_POINT_CARDINAL

    temp_fname = tmp_path / "test.fif"
    write_fiducials(temp_fname, pts, coord_frame)
    pts_1, coord_frame_1 = read_fiducials(temp_fname)
    assert coord_frame == coord_frame_1
    for pt, pt_1 in zip(pts, pts_1):
        assert pt["kind"] == pt_1["kind"]
        assert pt["ident"] == pt_1["ident"]
        assert pt["coord_frame"] == pt_1["coord_frame"]
        assert_array_equal(pt["r"], pt_1["r"])
        assert isinstance(pt, DigPoint)
        assert isinstance(pt_1, DigPoint)

    # test safeguards
    pts[0]["coord_frame"] += 1
    with pytest.raises(ValueError, match="coord_frame entries that are incom"):
        write_fiducials(temp_fname, pts, coord_frame, overwrite=True)


def test_info():
    """Test info object."""
    raw = read_raw_fif(raw_fname)
    event_id, tmin, tmax = 1, -0.2, 0.5
    events = read_events(event_name)
    event_id = int(events[0, 2])
    epochs = Epochs(raw, events[:1], event_id, tmin, tmax, picks=None)

    evoked = epochs.average()

    # Test subclassing was successful.
    info = Info(a=7, b="aaaaa")
    assert "a" in info
    assert "b" in info

    # Test info attribute in API objects
    for obj in [raw, epochs, evoked]:
        assert isinstance(obj.info, Info)
        rep = repr(obj.info)
        assert "2002-12-03 19:01:10 UTC" in rep, rep
        assert "146 items (3 Cardinal, 4 HPI, 61 EEG, 78 Extra)" in rep
        dig_rep = repr(obj.info["dig"][0])
        assert "LPA" in dig_rep, dig_rep
        assert "(-71.4, 0.0, 0.0) mm" in dig_rep, dig_rep
        assert "head frame" in dig_rep, dig_rep
        # Test our BunchConstNamed support
        for func in (str, repr):
            assert "4 (FIFFV_COORD_HEAD)" == func(obj.info["dig"][0]["coord_frame"])

    # Test read-only fields
    info = raw.info.copy()
    nchan = len(info["chs"])
    ch_names = [ch["ch_name"] for ch in info["chs"]]
    assert info["nchan"] == nchan
    assert list(info["ch_names"]) == ch_names

    # Deleting of regular fields should work
    info["experimenter"] = "bar"
    del info["experimenter"]

    # Test updating of fields
    del info["chs"][-1]
    info._update_redundant()
    assert info["nchan"] == nchan - 1
    assert list(info["ch_names"]) == ch_names[:-1]

    info["chs"][0]["ch_name"] = "foo"
    info._update_redundant()
    assert info["ch_names"][0] == "foo"

    # Test casting to and from a dict
    info_dict = dict(info)
    info2 = Info(info_dict)
    assert info == info2


def test_read_write_info(tmp_path):
    """Test IO of info."""
    info = read_info(raw_fname)
    temp_file = tmp_path / "info.fif"
    # check for bug `#1198`
    info["dev_head_t"]["trans"] = np.eye(4)
    t1 = info["dev_head_t"]["trans"]
    write_info(temp_file, info)
    info2 = read_info(temp_file)
    t2 = info2["dev_head_t"]["trans"]
    assert len(info["chs"]) == len(info2["chs"])
    assert_array_equal(t1, t2)
    # proc_history (e.g., GH#1875)
    creator = "é"
    info = read_info(chpi_fname)
    info["proc_history"][0]["creator"] = creator
    info["hpi_meas"][0]["creator"] = creator
    info["subject_info"]["his_id"] = creator
    info["subject_info"]["weight"] = 11.1
    info["subject_info"]["height"] = 2.3

    with info._unlock():
        if info["gantry_angle"] is None:  # future testing data may include it
            info["gantry_angle"] = 0.0  # Elekta supine position
    gantry_angle = info["gantry_angle"]

    meas_id = info["meas_id"]
    with pytest.raises(FileExistsError, match="Destination file exists"):
        write_info(temp_file, info)
    write_info(temp_file, info, overwrite=True)
    info = read_info(temp_file)
    assert info["proc_history"][0]["creator"] == creator
    assert info["hpi_meas"][0]["creator"] == creator
    assert info["subject_info"]["his_id"] == creator
    assert info["gantry_angle"] == gantry_angle
    assert_allclose(info["subject_info"]["height"], 2.3)
    assert_allclose(info["subject_info"]["weight"], 11.1)
    for key in ["secs", "usecs", "version"]:
        assert info["meas_id"][key] == meas_id[key]
    assert_array_equal(info["meas_id"]["machid"], meas_id["machid"])

    # Test that writing twice produces the same file
    m1 = _empty_hash()
    with open(temp_file, "rb") as fid:
        m1.update(fid.read())
    m1 = m1.hexdigest()
    temp_file_2 = tmp_path / "info2.fif"
    assert temp_file_2 != temp_file
    write_info(temp_file_2, info)
    m2 = _empty_hash()
    with open(str(temp_file_2), "rb") as fid:
        m2.update(fid.read())
    m2 = m2.hexdigest()
    assert m1 == m2

    info = read_info(raw_fname)
    with info._unlock():
        info["meas_date"] = None
    anonymize_info(info, verbose="error")
    assert info["meas_date"] is None
    tmp_fname_3 = tmp_path / "info3.fif"
    write_info(tmp_fname_3, info)
    assert info["meas_date"] is None
    info2 = read_info(tmp_fname_3)
    assert info2["meas_date"] is None

    # Check that having a very old date in fine until you try to save it to fif
    with info._unlock(check_after=True):
        info["meas_date"] = datetime(1800, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    fname = tmp_path / "test.fif"
    with pytest.raises(RuntimeError, match="must be between "):
        write_info(fname, info, overwrite=True)


@testing.requires_testing_data
def test_dir_warning():
    """Test that trying to read a bad filename emits a warning before an error."""
    with (
        pytest.raises(OSError, match="directory"),
        pytest.warns(RuntimeWarning, match="does not conform"),
    ):
        read_info(ctf_fname)


def test_io_dig_points(tmp_path):
    """Test Writing for dig files."""
    dest = tmp_path / "test.txt"
    points2 = np.array([[-106.93, 99.80], [99.80, 68.81]])
    np.savetxt(dest, points2, delimiter="\t", newline="\n")
    with pytest.raises(ValueError, match="must be of shape"):
        with pytest.warns(RuntimeWarning, match="FastSCAN header"):
            read_polhemus_fastscan(dest, on_header_missing="warn")


def test_io_coord_frame(tmp_path):
    """Test round trip for coordinate frame."""
    fname = tmp_path / "test.fif"
    for ch_type in ("eeg", "seeg", "ecog", "dbs", "hbo", "hbr"):
        info = create_info(ch_names=["Test Ch"], sfreq=1000.0, ch_types=[ch_type])
        info["chs"][0]["loc"][:3] = [0.05, 0.01, -0.03]
        write_info(fname, info, overwrite=True)
        info2 = read_info(fname)
        assert info2["chs"][0]["coord_frame"] == FIFF.FIFFV_COORD_HEAD


def test_make_dig_points():
    """Test application of Polhemus HSP to info."""
    extra_points = read_polhemus_fastscan(hsp_fname, on_header_missing="ignore")
    info = create_info(ch_names=["Test Ch"], sfreq=1000.0)
    assert info["dig"] is None

    with info._unlock():
        info["dig"] = _make_dig_points(extra_points=extra_points)
    assert info["dig"]
    assert_allclose(info["dig"][0]["r"], [-0.10693, 0.09980, 0.06881])

    elp_points = read_polhemus_fastscan(elp_fname, on_header_missing="ignore")
    nasion, lpa, rpa = elp_points[:3]
    info = create_info(ch_names=["Test Ch"], sfreq=1000.0)
    assert info["dig"] is None

    with info._unlock():
        info["dig"] = _make_dig_points(nasion, lpa, rpa, elp_points[3:], None)
    assert info["dig"]
    idx = [d["ident"] for d in info["dig"]].index(FIFF.FIFFV_POINT_NASION)
    assert_allclose(info["dig"][idx]["r"], [0.0013930, 0.0131613, -0.0046967])
    pytest.raises(ValueError, _make_dig_points, nasion[:2])
    pytest.raises(ValueError, _make_dig_points, None, lpa[:2])
    pytest.raises(ValueError, _make_dig_points, None, None, rpa[:2])
    pytest.raises(ValueError, _make_dig_points, None, None, None, elp_points[:, :2])
    pytest.raises(
        ValueError, _make_dig_points, None, None, None, None, elp_points[:, :2]
    )


def test_redundant():
    """Test some of the redundant properties of info."""
    # Indexing
    info = create_info(ch_names=["a", "b", "c"], sfreq=1000.0)
    assert info["ch_names"][0] == "a"
    assert info["ch_names"][1] == "b"
    assert info["ch_names"][2] == "c"

    # Equality
    assert info["ch_names"] == info["ch_names"]
    assert info["ch_names"] == ["a", "b", "c"]

    # No channels in info
    info = create_info(ch_names=[], sfreq=1000.0)
    assert info["ch_names"] == []

    # List should be read-only
    info = create_info(ch_names=["a", "b", "c"], sfreq=1000.0)


def test_merge_info():
    """Test merging of multiple Info objects."""
    info_a = create_info(ch_names=["a", "b", "c"], sfreq=1000.0)
    info_b = create_info(ch_names=["d", "e", "f"], sfreq=1000.0)
    info_merged = _merge_info([info_a, info_b])
    assert info_merged["nchan"], 6
    assert info_merged["ch_names"], ["a", "b", "c", "d", "e", "f"]
    pytest.raises(ValueError, _merge_info, [info_a, info_a])

    # Testing for force updates before merging
    info_c = create_info(ch_names=["g", "h", "i"], sfreq=500.0)
    # This will break because sfreq is not equal
    pytest.raises(RuntimeError, _merge_info, [info_a, info_c])
    _force_update_info(info_a, info_c)
    assert info_c["sfreq"] == info_a["sfreq"]
    assert info_c["ch_names"][0] != info_a["ch_names"][0]
    # Make sure it works now
    _merge_info([info_a, info_c])
    # Check that you must supply Info
    pytest.raises(ValueError, _force_update_info, info_a, dict([("sfreq", 1000.0)]))
    # KIT System-ID
    info_a._unlocked = info_b._unlocked = True
    info_a["kit_system_id"] = 50
    assert _merge_info((info_a, info_b))["kit_system_id"] == 50
    info_b["kit_system_id"] = 50
    assert _merge_info((info_a, info_b))["kit_system_id"] == 50
    info_b["kit_system_id"] = 60
    pytest.raises(ValueError, _merge_info, (info_a, info_b))

    # hpi infos
    info_d = create_info(ch_names=["d", "e", "f"], sfreq=1000.0)
    info_merged = _merge_info([info_a, info_d])
    assert not info_merged["hpi_meas"]
    assert not info_merged["hpi_results"]
    info_a["hpi_meas"] = [{"f1": 3, "f2": 4}]
    assert _merge_info([info_a, info_d])["hpi_meas"] == info_a["hpi_meas"]
    info_d._unlocked = True
    info_d["hpi_meas"] = [{"f1": 3, "f2": 4}]
    assert _merge_info([info_a, info_d])["hpi_meas"] == info_d["hpi_meas"]
    # This will break because of inconsistency
    info_d["hpi_meas"] = [{"f1": 3, "f2": 5}]
    pytest.raises(ValueError, _merge_info, [info_a, info_d])

    info_0 = read_info(raw_fname)
    info_0["bads"] = ["MEG 2443", "EEG 053"]
    assert len(info_0["chs"]) == 376
    assert len(info_0["dig"]) == 146
    info_1 = create_info(["STI YYY"], info_0["sfreq"], ["stim"])
    assert info_1["bads"] == []
    info_out = _merge_info([info_0, info_1], force_update_to_first=True)
    assert len(info_out["chs"]) == 377
    assert len(info_out["bads"]) == 2
    assert len(info_out["dig"]) == 146
    assert len(info_0["chs"]) == 376
    assert len(info_0["bads"]) == 2
    assert len(info_0["dig"]) == 146


def test_check_consistency():
    """Test consistency check of Info objects."""
    info = create_info(ch_names=["a", "b", "c"], sfreq=1000.0)

    # This should pass
    info._check_consistency()

    # Info without any channels
    info_empty = create_info(ch_names=[], sfreq=1000.0)
    info_empty._check_consistency()

    # Bad channels that are not in the info object
    info2 = info.copy()
    with pytest.raises(ValueError, match="do not exist"):
        info2["bads"] = ["b", "foo", "bar"]

    # Bad data types
    info2 = info.copy()
    with info2._unlock():
        info2["sfreq"] = "foo"
    pytest.raises(ValueError, info2._check_consistency)

    info2 = info.copy()
    with info2._unlock():
        info2["highpass"] = "foo"
    pytest.raises(ValueError, info2._check_consistency)

    info2 = info.copy()
    with info2._unlock():
        info2["lowpass"] = "foo"
    pytest.raises(ValueError, info2._check_consistency)

    # Silent type conversion to float
    info2 = info.copy()
    with info2._unlock(check_after=True):
        info2["sfreq"] = 1
        info2["highpass"] = 2
        info2["lowpass"] = 2
    assert isinstance(info2["sfreq"], float)
    assert isinstance(info2["highpass"], float)
    assert isinstance(info2["lowpass"], float)

    # Duplicate channel names
    info2 = info.copy()
    with info2._unlock():
        info2["chs"][2]["ch_name"] = "b"
    pytest.raises(RuntimeError, info2._check_consistency)

    # Duplicates appended with running numbers
    with pytest.warns(RuntimeWarning, match="Channel names are not"):
        info3 = create_info(ch_names=["a", "b", "b", "c", "b"], sfreq=1000.0)
    assert_array_equal(info3["ch_names"], ["a", "b-0", "b-1", "c", "b-2"])

    # a few bad ones
    idx = 0
    ch = info["chs"][idx]
    for key, bad, match in (
        ("ch_name", 1.0, "must be an instance"),
        ("loc", np.zeros(15), "12 elements"),
        ("cal", np.ones(1), "numeric"),
    ):
        info._check_consistency()  # okay
        old = ch[key]
        ch[key] = bad
        if key == "ch_name":
            info["ch_names"][idx] = bad
        with pytest.raises(TypeError, match=match):
            info._check_consistency()
        ch[key] = old
        if key == "ch_name":
            info["ch_names"][idx] = old

    # bad channel entries
    info2 = info.copy()
    info2["chs"][0]["foo"] = "bar"
    with pytest.raises(KeyError, match="key errantly present"):
        info2._check_consistency()
    info2 = info.copy()
    del info2["chs"][0]["loc"]
    with pytest.raises(KeyError, match="key missing"):
        info2._check_consistency()

    # bad subject_info entries
    info2 = info.copy()
    with pytest.raises(TypeError, match="must be an instance"):
        info2["subject_info"] = "bad"
    info2["subject_info"] = dict()
    with pytest.raises(TypeError, match="must be an instance"):
        info2["subject_info"]["height"] = "bad"
    with pytest.raises(TypeError, match="must be an instance"):
        info2["subject_info"]["weight"] = [0]
    with pytest.raises(TypeError, match=r'subject_info\["height"\] must be an .*'):
        info2["subject_info"] = {"height": "bad"}


def _test_anonymize_info(base_info, tmp_path):
    """Test that sensitive information can be anonymized."""
    pytest.raises(TypeError, anonymize_info, "foo")
    assert isinstance(base_info, Info)
    base_info = base_info.copy()

    default_anon_dos = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    default_str = "mne_anonymize"
    default_subject_id = 0
    default_desc = "Anonymized using a time shift" + " to preserve age at acquisition"

    # Test no error for incomplete info
    bad_info = base_info.copy()
    bad_info.pop("file_id")
    anonymize_info(bad_info)
    del bad_info

    # Fake some additional data
    _complete_info(base_info)
    meas_date = datetime(2010, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    with base_info._unlock():
        base_info["meas_date"] = meas_date
        base_info["subject_info"].update(
            birthday=date(1987, 4, 8),
            his_id="foobar",
            sex=0,
        )

    # generate expected info...
    # first expected result with no options.
    # will move DOS from 2010/1/1 to 2000/1/1 which is 3653 days.
    exp_info = base_info.copy()
    exp_info._unlocked = True
    exp_info["description"] = default_desc
    erase_strs = (
        ("experimenter",),
        ("proj_name",),
        ("subject_info", "first_name"),
        ("subject_info", "middle_name"),
        ("subject_info", "last_name"),
        ("device_info", "site"),
        ("device_info", "serial"),
        ("helium_info", "orig_file_guid"),
        ("proc_history", 0, "experimenter"),
    )
    for tp in erase_strs:
        this = exp_info
        for lev in tp[:-1]:
            this = this[lev]
        this[tp[-1]] = default_str
    exp_info["proj_id"] = 0
    for key in ("sex", "id", "height", "weight"):
        exp_info["subject_info"][key] = 0
    exp_info["subject_info"]["his_id"] = str(default_subject_id)
    del exp_info["subject_info"]["hand"]  # there's no "unknown" setting
    exp_info["utc_offset"] = None
    exp_info["proc_history"][0]["block_id"]["machid"][:] = 0

    # this bday is 3653 days different. the change in day is due to a
    # different number of leap days between 1987 and 1977 than between
    # 2010 and 2000.
    exp_info["subject_info"]["birthday"] = date(1977, 4, 7)
    exp_info["meas_date"] = default_anon_dos
    exp_info._unlocked = False

    # make copies
    exp_info_3 = exp_info.copy()

    # adjust each expected outcome
    delta_t = timedelta(days=3653)

    def _adjust_back(e_i, dt):
        for key in ("file_id", "meas_id"):
            value = e_i.get(key)
            if value is not None:
                assert "msecs" not in value
                tmp = _add_timedelta_to_stamp((value["secs"], value["usecs"]), -dt)
                value["secs"] = tmp[0]
                value["usecs"] = tmp[1]
                value["machid"][:] = 0
        e_i["helium_info"]["meas_date"] -= dt
        ds = int(round(dt.total_seconds()))
        e_i["proc_history"][0]["date"] = (
            e_i["proc_history"][0]["date"][0] - ds,
            e_i["proc_history"][0]["date"][1],
        )
        e_i["proc_history"][0]["block_id"]["secs"] -= ds

    _adjust_back(exp_info, delta_t)

    # exp 2 tests the keep_his option
    exp_info_2 = exp_info.copy()
    with exp_info_2._unlock():
        exp_info_2["subject_info"]["his_id"] = "foobar"
        exp_info_2["subject_info"]["sex"] = 0
        exp_info_2["subject_info"]["hand"] = 1

    # exp 3 tests is a supplied daysback
    delta_t_2 = timedelta(days=43)
    with exp_info_3._unlock():
        exp_info_3["subject_info"]["birthday"] = date(1987, 2, 24)
        exp_info_3["meas_date"] = meas_date - delta_t_2
    _adjust_back(exp_info_3, delta_t_2)

    # exp 4 tests is a supplied daysback
    delta_t_3 = timedelta(days=223 + 364 * 500)

    def _check_equiv(got, want, err_msg):
        __tracebackhide__ = True
        fname_temp = tmp_path / "test.fif"
        assert_object_equal(got, want, err_msg=err_msg)
        write_info(fname_temp, got, reset_range=False, overwrite=True)
        got = read_info(fname_temp)
        # this gets changed on write but that's expected
        with got._unlock():
            got["file_id"] = want["file_id"]
        assert_object_equal(got, want, err_msg=f"{err_msg} (on I/O round trip)")

    new_info = anonymize_info(base_info.copy())
    _check_equiv(new_info, exp_info, err_msg="anon mismatch")

    new_info = anonymize_info(base_info.copy(), keep_his=True)
    _check_equiv(new_info, exp_info_2, err_msg="anon keep_his mismatch")

    new_info = anonymize_info(base_info.copy(), daysback=delta_t_2.days)
    _check_equiv(new_info, exp_info_3, err_msg="anon daysback mismatch")

    with pytest.raises(RuntimeError, match="anonymize_info generated"):
        anonymize_info(base_info.copy(), daysback=delta_t_3.days)
    # assert_object_equal(new_info, exp_info_4)

    # test with meas_date = None
    with base_info._unlock():
        base_info["meas_date"] = None
    with exp_info_3._unlock():
        exp_info_3["meas_date"] = None
        exp_info_3["helium_info"]["meas_date"] = None
        for var in (
            exp_info_3["file_id"],
            exp_info_3["meas_id"],
            exp_info_3["proc_history"][0]["block_id"],
        ):
            var["secs"] = DATE_NONE[0]
            var["usecs"] = DATE_NONE[1]
        exp_info_3["subject_info"].pop("birthday", None)
        exp_info_3["proc_history"][0]["date"] = DATE_NONE

    if base_info["meas_date"] is None:
        with pytest.warns(RuntimeWarning, match="all information"):
            new_info = anonymize_info(base_info.copy(), daysback=delta_t_2.days)
    else:
        new_info = anonymize_info(base_info.copy(), daysback=delta_t_2.days)
    _check_equiv(
        new_info,
        exp_info_3,
        err_msg="meas_date=None daysback mismatch",
    )

    with _record_warnings():  # meas_date is None
        new_info = anonymize_info(base_info.copy())
    _check_equiv(new_info, exp_info_3, err_msg="meas_date=None mismatch")


@pytest.mark.parametrize(
    "stamp, dt",
    [
        [(1346981585, 835782), (2012, 9, 7, 1, 33, 5, 835782)],
        # test old dates for BIDS anonymization
        [(-1533443343, 24382), (1921, 5, 29, 19, 30, 57, 24382)],
        # gh-7116
        [(-908196946, 988669), (1941, 3, 22, 11, 4, 14, 988669)],
    ],
)
def test_meas_date_convert(stamp, dt):
    """Test conversions of meas_date to datetime objects."""
    meas_datetime = _stamp_to_dt(stamp)
    stamp2 = _dt_to_stamp(meas_datetime)
    assert stamp == stamp2
    assert meas_datetime == datetime(*dt, tzinfo=timezone.utc)
    # smoke test for info __repr__
    info = create_info(1, 1000.0, "eeg")
    with info._unlock():
        info["meas_date"] = meas_datetime
    assert str(dt[0]) in repr(info)


def test_birthday_input():
    """Test that birthday input is handled correctly."""
    pd = pytest.importorskip("pandas")

    # Test valid date
    info = create_info(ch_names=["EEG 001"], sfreq=1000.0, ch_types="eeg")
    info["subject_info"] = {}
    info["subject_info"]["birthday"] = date(2000, 1, 1)
    assert info["subject_info"]["birthday"] == date(2000, 1, 1)

    # pandas Timestamp should convert to datetime date
    info["subject_info"]["birthday"] = pd.Timestamp("2000-01-01")
    assert info["subject_info"]["birthday"] == date(2000, 1, 1)
    # Ensure we've converted it during setting
    assert not isinstance(info["subject_info"]["birthday"], pd.Timestamp)

    # Test invalid date raises error
    with pytest.raises(TypeError, match="must be an instance of date"):
        info["subject_info"]["birthday"] = "not a date"


def _complete_info(info):
    """Complete the meas info fields."""
    for key in ("file_id", "meas_id"):
        assert info[key] is not None
    info["subject_info"] = dict(
        id=1,
        sex=1,
        hand=1,
        first_name="a",
        middle_name="b",
        last_name="c",
        his_id="d",
        birthday=date(2000, 1, 1),
        weight=1.0,
        height=2.0,
    )
    info["helium_info"] = dict(
        he_level_raw=np.float32(12.34),
        helium_level=np.float32(45.67),
        meas_date=datetime(2024, 11, 14, 14, 8, 2, tzinfo=timezone.utc),
        orig_file_guid="e",
    )
    info["experimenter"] = "f"
    info["description"] = "g"
    with info._unlock():
        info["proj_id"] = 1
        info["proj_name"] = "h"
        info["utc_offset"] = "i"
        d = (1717707794, 2)
        info["proc_history"] = [
            dict(
                block_id=dict(
                    version=4,
                    machid=np.ones(2, int),
                    secs=d[0],
                    usecs=d[1],
                ),
                experimenter="j",
                max_info=dict(
                    max_st=dict(),
                    sss_ctc=dict(),
                    sss_cal=dict(),
                    sss_info=dict(in_order=8),
                ),
                date=d,
            ),
        ]
        info["device_info"] = dict(serial="k", site="l")
    info._check_consistency()


def test_anonymize(tmp_path):
    """Test that sensitive information can be anonymized."""
    pytest.raises(TypeError, anonymize_info, "foo")

    # Fake some subject data
    raw = read_raw_fif(raw_fname)
    _complete_info(raw.info)
    raw.set_annotations(
        Annotations(onset=[0, 1], duration=[1, 1], description="dummy", orig_time=None)
    )
    first_samp = raw.first_samp
    expected_onset = np.arange(2) + raw._first_time
    assert raw.first_samp == first_samp
    assert_allclose(raw.annotations.onset, expected_onset)

    # test mne.anonymize_info()
    events = read_events(event_name)
    epochs = Epochs(raw, events[:1], 2, 0.0, 0.1, baseline=None)
    _test_anonymize_info(raw.info, tmp_path)
    _test_anonymize_info(epochs.info, tmp_path)

    # test instance methods & I/O roundtrip
    for inst, keep_his in zip((raw, epochs), (True, False)):
        inst = inst.copy()

        subject_info = dict(his_id="Volunteer", sex=2, hand=1)
        inst.info["subject_info"] = subject_info
        inst.anonymize(keep_his=keep_his)

        si = inst.info["subject_info"]
        if keep_his:
            assert si == subject_info
        else:
            assert si["his_id"] == "0"
            assert si["sex"] == 0
            assert "hand" not in si

        # write to disk & read back
        inst_type = "raw" if isinstance(inst, BaseRaw) else "epo"
        fname = "tmp_raw.fif" if inst_type == "raw" else "tmp_epo.fif"
        out_path = tmp_path / fname
        inst.save(out_path, overwrite=True)
        if inst_type == "raw":
            read_raw_fif(out_path)
        else:
            read_epochs(out_path)

    # test that annotations are correctly zeroed
    raw.anonymize()
    assert raw.first_samp == first_samp
    assert_allclose(raw.annotations.onset, expected_onset)
    assert raw.annotations.orig_time == raw.info["meas_date"]
    stamp = _dt_to_stamp(raw.info["meas_date"])
    assert raw.annotations.orig_time == _stamp_to_dt(stamp)

    with raw.info._unlock():
        raw.info["meas_date"] = None
    raw.anonymize(daysback=None)
    with pytest.warns(RuntimeWarning, match="None"):
        raw.anonymize(daysback=123)
    assert raw.annotations.orig_time is None
    assert raw.first_samp == first_samp
    assert_allclose(raw.annotations.onset, expected_onset)


@pytest.mark.parametrize("daysback", [None, 28826])
def test_anonymize_with_io(tmp_path, daysback):
    """Test that IO does not break anonymization and all fields."""
    raw = read_raw_fif(raw_fname).crop(0, 1)
    _complete_info(raw.info)
    temp_path = tmp_path / "tmp_raw.fif"
    raw.save(temp_path)
    raw2 = read_raw_fif(temp_path).load_data()
    raw2.anonymize(daysback=daysback)
    raw2.save(temp_path, overwrite=True)
    raw3 = read_raw_fif(temp_path)
    d = object_diff(raw2.info, raw3.info)
    assert d == "['file_id']['machid'] array mismatch\n"


@testing.requires_testing_data
def test_csr_csc(tmp_path):
    """Test CSR and CSC."""
    info = read_info(sss_ctc_fname)
    info = pick_info(info, pick_types(info, meg=True, exclude=[]))
    sss_ctc = info["proc_history"][0]["max_info"]["sss_ctc"]
    ct = sss_ctc["decoupler"].copy()
    # CSC
    assert isinstance(ct, sparse.csc_array)
    fname = tmp_path / "test.fif"
    write_info(fname, info)
    info_read = read_info(fname)
    ct_read = info_read["proc_history"][0]["max_info"]["sss_ctc"]["decoupler"]
    assert isinstance(ct_read, sparse.csc_array)
    assert_array_equal(ct_read.toarray(), ct.toarray())
    # Now CSR
    csr = ct.tocsr()
    assert isinstance(csr, sparse.csr_array)
    assert_array_equal(csr.toarray(), ct.toarray())
    info["proc_history"][0]["max_info"]["sss_ctc"]["decoupler"] = csr
    fname = tmp_path / "test1.fif"
    write_info(fname, info)
    info_read = read_info(fname)
    ct_read = info_read["proc_history"][0]["max_info"]["sss_ctc"]["decoupler"]
    assert isinstance(ct_read, sparse.csc_array)  # this gets cast to CSC
    assert_array_equal(ct_read.toarray(), ct.toarray())


@testing.requires_testing_data
def test_check_compensation_consistency():
    """Test check picks compensation."""
    raw = read_raw_ctf(ctf_fname, preload=False)
    events = make_fixed_length_events(raw, 99999)
    picks = pick_types(raw.info, meg=True, exclude=[], ref_meg=True)
    pick_ch_names = [raw.info["ch_names"][idx] for idx in picks]
    for comp, expected_result in zip([0, 1], [False, False]):
        raw.apply_gradient_compensation(comp)
        ret, missing = _bad_chans_comp(raw.info, pick_ch_names)
        assert ret == expected_result
        assert len(missing) == 0
        Epochs(raw, events, None, -0.2, 0.2, preload=False, picks=picks)

    picks = pick_types(raw.info, meg=True, exclude=[], ref_meg=False)
    pick_ch_names = [raw.info["ch_names"][idx] for idx in picks]

    for comp, expected_result in zip([0, 1], [False, True]):
        raw.apply_gradient_compensation(comp)
        ret, missing = _bad_chans_comp(raw.info, pick_ch_names)
        assert ret == expected_result
        assert len(missing) == 17
        with catch_logging() as log:
            Epochs(
                raw, events, None, -0.2, 0.2, preload=False, picks=picks, verbose=True
            )
            assert "Removing 5 compensators" in log.getvalue()


def test_field_round_trip(tmp_path):
    """Test round-trip for new fields."""
    info = create_info(1, 1000.0, "eeg")
    with info._unlock():
        for key in ("file_id", "meas_id"):
            info[key] = _generate_meas_id()
        info["device_info"] = dict(type="a", model="b", serial="c", site="d")
        info["helium_info"] = dict(
            he_level_raw=1.0,
            helium_level=2.0,
            orig_file_guid="e",
            meas_date=_stamp_to_dt((1, 2)),
        )
    fname = tmp_path / "temp-info.fif"
    info.save(fname)
    info_read = read_info(fname)
    assert_object_equal(info, info_read)
    with pytest.raises(TypeError, match="datetime"):
        info["helium_info"]["meas_date"] = (1, 2)
    # should allow it to be None, though (checking gh-13154)
    info["helium_info"]["meas_date"] = None
    info.save(fname, overwrite=True)
    info_read = read_info(fname)
    assert_object_equal(info, info_read)
    assert info_read["helium_info"]["meas_date"] is None
    # not 100% sure how someone could end up with it deleted, but should still be
    # writeable
    del info["helium_info"]["meas_date"]
    info.save(fname, overwrite=True)
    info_read = read_info(fname)
    info["helium_info"]["meas_date"] = None  # we always set it (which is reasonable)
    assert_object_equal(info, info_read)


def test_equalize_channels():
    """Test equalization of channels for instances of Info."""
    info1 = create_info(["CH1", "CH2", "CH3"], sfreq=1.0)
    info2 = create_info(["CH4", "CH2", "CH1"], sfreq=1.0)
    info1, info2 = equalize_channels([info1, info2])

    assert info1.ch_names == ["CH1", "CH2"]
    assert info2.ch_names == ["CH1", "CH2"]


def test_repr():
    """Test Info repr."""
    info = create_info(1, 1000, "eeg")
    assert "7 non-empty values" in repr(info)

    t = Transform("meg", "head", np.ones((4, 4)))
    info["dev_head_t"] = t
    assert "dev_head_t: MEG device -> head transform" in repr(info)


def test_repr_html():
    """Test Info HTML repr."""
    info = read_info(raw_fname)
    assert "Projections" in info._repr_html_()
    with info._unlock():
        info["projs"] = []
    assert "Projections" not in info._repr_html_()
    info["bads"] = []
    assert "Bad " not in info._repr_html_()
    info["bads"] = ["MEG 2443", "EEG 053"]
    assert "Bad " in info._repr_html_()  # 1 for each channel type

    html = info._repr_html_()
    for ch in [  # good channel counts
        "203",  # grad
        "102",  # mag
        "9",  # stim
        "59",  # eeg
        "1",  # eog
    ]:
        assert ch in html


@testing.requires_testing_data
def test_invalid_subject_birthday():
    """Test handling of an invalid birthday in the raw file."""
    with pytest.warns(RuntimeWarning, match="No birthday will be set"):
        raw = read_raw_fif(raw_invalid_bday_fname)
    assert "birthday" not in raw.info["subject_info"]


@pytest.mark.parametrize(
    "fname",
    [
        pytest.param(ctf_fname, marks=testing._pytest_mark()),
        raw_fname,
    ],
)
def test_channel_name_limit(tmp_path, monkeypatch, fname):
    """Test that our remapping works properly."""
    #
    # raw
    #
    if fname.suffix == ".fif":
        raw = read_raw_fif(fname)
        raw.pick(raw.ch_names[:3])
        ref_names = []
        data_names = raw.ch_names
    else:
        assert fname.suffix == ".ds"
        raw = read_raw_ctf(fname)
        ref_names = [
            raw.ch_names[pick] for pick in pick_types(raw.info, meg=False, ref_meg=True)
        ]
        data_names = raw.ch_names[32:35]
    proj = dict(
        data=np.ones((1, len(data_names))),
        col_names=data_names[:2].copy(),
        row_names=None,
        nrow=1,
    )
    proj = Projection(data=proj, active=False, desc="test", kind=0, explained_var=0.0)
    raw.add_proj(proj, remove_existing=True)
    raw.info.normalize_proj()
    raw.pick(data_names + ref_names).crop(0, 2)
    long_names = ["123456789abcdefg" + name for name in raw.ch_names]
    fname = tmp_path / "test-raw.fif"
    with catch_logging() as log:
        raw.save(fname)
    log = log.getvalue()
    assert "truncated" not in log
    rename = dict(zip(raw.ch_names, long_names))
    long_data_names = [rename[name] for name in data_names]
    long_proj_names = long_data_names[:2]
    raw.rename_channels(rename)
    for comp in raw.info["comps"]:
        for key in ("row_names", "col_names"):
            for name in comp["data"][key]:
                assert name in raw.ch_names
    if raw.info["comps"]:
        assert raw.compensation_grade == 0
        raw.apply_gradient_compensation(3)
        assert raw.compensation_grade == 3
    assert len(raw.info["projs"]) == 1
    assert raw.info["projs"][0]["data"]["col_names"] == long_proj_names
    raw.info["bads"] = bads = long_data_names[2:3]
    good_long_data_names = [name for name in long_data_names if name not in bads]
    with catch_logging() as log:
        raw.save(fname, overwrite=True, verbose=True)
    log = log.getvalue()
    assert "truncated to 15" in log
    for name in raw.ch_names:
        assert len(name) > 15
    # first read the full way
    with catch_logging() as log:
        raw_read = read_raw_fif(fname, verbose=True)
    log = log.getvalue()
    assert "Reading extended channel information" in log
    for ra in (raw, raw_read):
        assert ra.ch_names == long_names
    assert raw_read.info["projs"][0]["data"]["col_names"] == long_proj_names
    del raw_read
    # next read as if no longer names could be read
    monkeypatch.setattr(meas_info, "_read_extended_ch_info", lambda x, y, z: None)
    with catch_logging() as log:
        raw_read = read_raw_fif(fname, verbose=True)
    log = log.getvalue()
    assert "extended" not in log
    if raw.info["comps"]:
        assert raw_read.compensation_grade == 3
        raw_read.apply_gradient_compensation(0)
        assert raw_read.compensation_grade == 0
    monkeypatch.setattr(  # restore
        meas_info, "_read_extended_ch_info", _read_extended_ch_info
    )
    short_proj_names = [
        f"{name[: 13 - bool(len(ref_names))]}-{ni}"
        for ni, name in enumerate(long_proj_names)
    ]
    assert raw_read.info["projs"][0]["data"]["col_names"] == short_proj_names
    #
    # epochs
    #
    epochs = Epochs(raw, make_fixed_length_events(raw))
    fname = tmp_path / "test-epo.fif"
    epochs.save(fname)
    epochs_read = read_epochs(fname)
    for ep in (epochs, epochs_read):
        assert ep.info["ch_names"] == long_names
        assert ep.ch_names == long_names
    del raw, epochs_read
    # cov
    epochs.info["bads"] = []
    cov = compute_covariance(epochs, verbose="error")
    fname = tmp_path / "test-cov.fif"
    write_cov(fname, cov)
    cov_read = read_cov(fname)
    for co in (cov, cov_read):
        assert co["names"] == long_data_names
        assert co["bads"] == []
    del cov_read

    #
    # evoked
    #
    evoked = epochs.average()
    evoked.info["bads"] = bads
    assert evoked.nave == 1
    fname = tmp_path / "test-ave.fif"
    evoked.save(fname)
    evoked_read = read_evokeds(fname)[0]
    for ev in (evoked, evoked_read):
        assert ev.ch_names == long_names
        assert ev.info["bads"] == bads
    del evoked_read, epochs

    #
    # forward
    #
    with _record_warnings():  # not enough points for CTF
        sphere = make_sphere_model("auto", "auto", evoked.info)
    src = setup_volume_source_space(pos=dict(rr=[[0, 0, 0.04]], nn=[[0, 1.0, 0.0]]))
    fwd = make_forward_solution(evoked.info, None, src, sphere)
    fname = tmp_path / "temp-fwd.fif"
    write_forward_solution(fname, fwd)
    fwd_read = read_forward_solution(fname)
    for fw in (fwd, fwd_read):
        assert fw["sol"]["row_names"] == long_data_names
        assert fw["info"]["ch_names"] == long_data_names
        assert fw["info"]["bads"] == bads
    del fwd_read

    #
    # inv
    #
    inv = make_inverse_operator(evoked.info, fwd, cov)
    fname = tmp_path / "test-inv.fif"
    write_inverse_operator(fname, inv)
    inv_read = read_inverse_operator(fname)
    for iv in (inv, inv_read):
        assert iv["info"]["ch_names"] == good_long_data_names
    apply_inverse(evoked, inv)  # smoke test


@pytest.mark.parametrize("protocol", ("highest", "default"))
@pytest.mark.parametrize("fname_info", (raw_fname, "create_info"))
@pytest.mark.parametrize("unlocked", (True, False))
def test_pickle(fname_info, unlocked, protocol):
    """Test that Info can be (un)pickled."""
    if fname_info == "create_info":
        info = create_info(3, 1000.0, "eeg")
    else:
        info = read_info(fname_info)
    protocol = getattr(pickle, f"{protocol.upper()}_PROTOCOL")
    assert isinstance(info["bads"], MNEBadsList)
    info["bads"] = info["ch_names"][:1]
    assert not info._unlocked
    info._unlocked = unlocked
    data = pickle.dumps(info, protocol=protocol)
    info_un = pickle.loads(data)  # nosec B301
    assert isinstance(info_un, Info)
    assert_object_equal(info, info_un)
    assert info_un._unlocked == unlocked
    assert isinstance(info_un["bads"], MNEBadsList)
    assert info_un["bads"]._mne_info is info_un


def test_info_bad():
    """Test our info sanity checkers."""
    info = create_info(5, 1000.0, "eeg")
    info["description"] = "foo"
    info["experimenter"] = "bar"
    info["line_freq"] = 50.0
    info["bads"] = info["ch_names"][:1]
    info["temp"] = ("whatever", 1.0)

    with pytest.raises(RuntimeError, match=r"info\['temp'\]"):
        info["bad_key"] = 1.0
    for key, match in [("sfreq", r"inst\.resample"), ("chs", r"inst\.add_channels")]:
        with pytest.raises(RuntimeError, match=match):
            info[key] = info[key]
    with pytest.raises(ValueError, match="between meg<->head"):
        info["dev_head_t"] = Transform("mri", "head", np.eye(4))
    assert isinstance(info["bads"], MNEBadsList)
    with pytest.raises(ValueError, match="do not exist in info"):
        info["bads"] = ["foo"]
    assert isinstance(info["bads"], MNEBadsList)
    with pytest.raises(ValueError, match="do not exist in info"):
        info["bads"] += ["foo"]
    assert isinstance(info["bads"], MNEBadsList)
    with pytest.raises(ValueError, match="do not exist in info"):
        info["bads"].append("foo")
    assert isinstance(info["bads"], MNEBadsList)
    with pytest.raises(ValueError, match="do not exist in info"):
        info["bads"].extend(["foo"])
    assert isinstance(info["bads"], MNEBadsList)
    x = info["bads"]
    with pytest.raises(ValueError, match="do not exist in info"):
        x.append("foo")
    assert info["bads"] == info["ch_names"][:1]  # unchonged
    x = info["bads"] + info["ch_names"][1:2]
    assert x == info["ch_names"][:2]
    assert not isinstance(x, MNEBadsList)  # plain list
    x = info["ch_names"][1:2] + info["bads"]
    assert x == info["ch_names"][1::-1]  # like [1, 0] in fancy indexing
    assert not isinstance(x, MNEBadsList)  # plain list


def test_get_montage():
    """Test ContainsMixin.get_montage()."""
    ch_names = make_standard_montage("standard_1020").ch_names
    sfreq = 512
    data = np.zeros((len(ch_names), sfreq * 2))
    raw = RawArray(data, create_info(ch_names, sfreq, "eeg"))
    raw.set_montage("standard_1020")

    assert len(raw.get_montage().ch_names) == len(ch_names)
    raw.info["bads"] = [ch_names[0]]
    assert len(raw.get_montage().ch_names) == len(ch_names)

    # test info
    raw = RawArray(data, create_info(ch_names, sfreq, "eeg"))
    raw.set_montage("standard_1020")

    assert len(raw.info.get_montage().ch_names) == len(ch_names)
    raw.info["bads"] = [ch_names[0]]
    assert len(raw.info.get_montage().ch_names) == len(ch_names)


def test_tag_consistency():
    """Test that structures for tag reading are consistent."""
    call_set = set(tag._call_dict)
    call_names = set(tag._call_dict_names)
    assert call_set == call_names, "Mismatch between _call_dict and _call_dict_names"
    # TODO: This was inspired by FIFF_DIG_STRING gh-13083, we should ideally add a test
    # that those dig points can actually be read in correctly at some point.


def test_proj_id_entries():
    """Test that proj_id entries are the right type."""
    info = create_info(5, 1000.0, "eeg")
    info["proj_id"] = 123
    # Boolean should be cast into an int
    info["proj_id"] = True
    with pytest.raises(TypeError, match="must be an instance"):
        info["proj_id"] = "bad"
    with pytest.raises(TypeError, match="must be an instance"):
        info["proj_id"] = np.array([123])
