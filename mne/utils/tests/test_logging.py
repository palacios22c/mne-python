# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import logging
import os
import re
import warnings
from pathlib import Path

import numpy as np
import pytest

from mne import Epochs, create_info, read_evokeds
from mne.io import RawArray, read_raw_fif
from mne.parallel import parallel_func
from mne.utils import (
    _get_call_line,
    catch_logging,
    check,
    filter_out_warnings,
    logger,
    set_log_file,
    set_log_level,
    use_log_level,
    verbose,
    warn,
)
from mne.utils._logging import _frame_info

base_dir = Path(__file__).parents[2] / "io" / "tests" / "data"
fname_raw = base_dir / "test_raw.fif"
fname_evoked = base_dir / "test-ave.fif"
fname_log = base_dir / "test-ave.log"
fname_log_2 = base_dir / "test-ave-2.log"


@verbose
def _fun(verbose=None):
    logger.debug("Test")


def test_frame_info(capsys, monkeypatch):
    """Test _frame_info."""
    stack = _frame_info(100)
    assert 2 < len(stack) < 100
    this, pytest_line = stack[:2]
    assert re.match("^test_logging:[1-9][0-9]$", this) is not None, this
    assert "pytest" in pytest_line
    capsys.readouterr()
    with use_log_level("debug", add_frames=4):
        _fun()
    out, _ = capsys.readouterr()
    out = out.replace("\n", " ")
    assert (
        re.match(
            ".*pytest.*test_logging:[2-9][0-9] .*test_logging:[1-9][0-9] :.*Test",
            out,
        )
        is not None
    ), this
    monkeypatch.setattr("inspect.currentframe", lambda: None)
    assert _frame_info(1) == ["unknown"]


def test_how_to_deal_with_warnings():
    """Test filter some messages out of warning records."""
    with pytest.warns(Warning, match="(bb|aa) warning") as w:
        warnings.warn("aa warning", UserWarning)
        warnings.warn("bb warning", UserWarning)
        warnings.warn("bb warning", RuntimeWarning)
        warnings.warn("aa warning", UserWarning)
    filter_out_warnings(w, category=UserWarning, match="aa")
    filter_out_warnings(w, category=RuntimeWarning)
    assert len(w) == 1


def clean_lines(lines=()):
    """Scrub filenames for checking logging output (in test_logging)."""
    return [line if "Reading " not in line else "Reading test file" for line in lines]


def test_logging_options(tmp_path):
    """Test logging (to file)."""
    with use_log_level(None):  # just ensure it's set back
        with pytest.raises(ValueError, match="Invalid value for the 'verbose"):
            set_log_level("foo")
        test_name = tmp_path / "test.log"
        with open(fname_log) as old_log_file:
            # [:-1] used to strip an extra "No baseline correction applied"
            old_lines = clean_lines(old_log_file.readlines())
            old_lines.pop(-1)
        with open(fname_log_2) as old_log_file_2:
            old_lines_2 = clean_lines(old_log_file_2.readlines())
            old_lines_2.pop(14)
            old_lines_2.pop(-1)

        if test_name.is_file():
            os.remove(test_name)
        # test it one way (printing default off)
        set_log_file(test_name)
        set_log_level("WARNING")
        # should NOT print
        evoked = read_evokeds(fname_evoked, condition=1)
        with open(test_name) as fid:
            assert fid.readlines() == []
        # should NOT print
        evoked = read_evokeds(fname_evoked, condition=1, verbose=False)
        with open(test_name) as fid:
            assert fid.readlines() == []
        # should NOT print
        evoked = read_evokeds(fname_evoked, condition=1, verbose="WARNING")
        with open(test_name) as fid:
            assert fid.readlines() == []
        # SHOULD print
        evoked = read_evokeds(fname_evoked, condition=1, verbose=True)
        with open(test_name) as new_log_file:
            new_lines = clean_lines(new_log_file.readlines())
        assert new_lines == old_lines
        set_log_file(None)  # Need to do this to close the old file
        os.remove(test_name)

        # now go the other way (printing default on)
        set_log_file(test_name)
        set_log_level("INFO")
        # should NOT print
        evoked = read_evokeds(fname_evoked, condition=1, verbose="WARNING")
        with open(test_name) as fid:
            assert fid.readlines() == []
        # should NOT print
        evoked = read_evokeds(fname_evoked, condition=1, verbose=False)
        with open(test_name) as fid:
            assert fid.readlines() == []
        # SHOULD print
        evoked = read_evokeds(fname_evoked, condition=1)
        with open(test_name) as new_log_file:
            new_lines = clean_lines(new_log_file.readlines())
        assert new_lines == old_lines
        # check to make sure appending works (and as default, raises a warning)
        set_log_file(test_name, overwrite=False)
        with pytest.warns(RuntimeWarning, match="appended to the file"):
            set_log_file(test_name)
        evoked = read_evokeds(fname_evoked, condition=1)
        with open(test_name) as new_log_file:
            new_lines = clean_lines(new_log_file.readlines())
        assert new_lines == old_lines_2

        # make sure overwriting works
        set_log_file(test_name, overwrite=True)
        # this line needs to be called to actually do some logging
        evoked = read_evokeds(fname_evoked, condition=1)
        del evoked
        with open(test_name) as new_log_file:
            new_lines = clean_lines(new_log_file.readlines())
        assert new_lines == old_lines
    with catch_logging() as log:
        pass
    assert log.getvalue() == ""


@pytest.mark.parametrize("verbose", (True, False))
def test_verbose_method(verbose):
    """Test for gh-8772."""
    # raw
    raw = read_raw_fif(fname_raw, verbose=verbose)
    with catch_logging() as log:
        raw.load_data(verbose=True)
    log = log.getvalue()
    assert "Reading 0 ... 14399" in log
    with catch_logging() as log:
        raw.load_data(verbose=False)
    log = log.getvalue()
    assert log == ""
    # epochs
    events = np.array([[raw.first_samp + 200, 0, 1]], int)
    epochs = Epochs(raw, events, verbose=verbose)
    with catch_logging() as log:
        epochs.drop_bad(verbose=True)
    log = log.getvalue()
    assert "0 bad epochs dropped" in log
    epochs = Epochs(raw, events, verbose=verbose)
    with catch_logging() as log:
        epochs.drop_bad(verbose=False)
    log = log.getvalue()
    assert log == ""


def test_warn(capsys, tmp_path, monkeypatch):
    """Test the smart warn() function."""
    with pytest.warns(RuntimeWarning, match="foo"):
        warn("foo")
    captured = capsys.readouterr()
    assert captured.out == ""  # gh-5592
    assert captured.err == ""  # this is because pytest.warns took it already
    # test ignore_namespaces
    bad_name = tmp_path / "bad.fif"
    raw = RawArray(np.zeros((1, 1)), create_info(1, 1000.0, "eeg"))
    with pytest.warns(RuntimeWarning, match="filename") as ws:
        raw.save(bad_name)
    assert len(ws) == 1
    assert "test_logging.py" in ws[0].filename  # this file (it's in tests/)

    def warn_wrap(msg):
        warn(msg, ignore_namespaces=())

    monkeypatch.setattr(check, "warn", warn_wrap)
    with pytest.warns(RuntimeWarning, match="filename") as ws:
        raw.save(bad_name, overwrite=True)

    assert len(ws) == 1
    assert "test_logging.py" not in ws[0].filename  # this file
    assert "_logging.py" in ws[0].filename  # where `mne.utils.warn` lives


def test_get_call_line():
    """Test getting a call line."""

    @verbose
    def foo(verbose=None):
        return _get_call_line()

    for v in (None, True):
        my_line = foo(verbose=v)  # testing
        assert my_line == "my_line = foo(verbose=v)  # testing"

    def bar():
        return _get_call_line()

    my_line = bar()  # testing more
    assert my_line == "my_line = bar()  # testing more"


def test_verbose_strictness():
    """Test that the verbose decorator is strict about usability."""

    @verbose
    def bad_verbose():
        pass

    with pytest.raises(RuntimeError, match="does not accept"):
        bad_verbose()

    class Okay:
        @verbose
        def meth_2(self, verbose=None):
            logger.info("meth_2")

    o = Okay()
    with catch_logging() as log:
        o.meth_2(verbose=False)
    log = log.getvalue()
    assert log == ""
    with catch_logging() as log:
        o.meth_2(verbose=True)
    log = log.getvalue()
    assert "meth_2" in log
    with catch_logging() as log:
        o.meth_2(verbose=False)
    log = log.getvalue()
    assert log == ""


@pytest.mark.parametrize("n_jobs", (1, 2))
def test_verbose_threads(n_jobs):
    """Test that our verbose level propagates to threads."""

    def my_fun():
        from mne.utils import logger

        return logger.level

    with use_log_level("info"):
        assert logger.level == logging.INFO
        with use_log_level("warning"):
            assert logger.level == logging.WARNING
            parallel, p_fun, got_jobs = parallel_func(my_fun, n_jobs=n_jobs)
            assert got_jobs in (1, n_jobs)  # FORCE_SERIAL could be set
            out = parallel(p_fun() for _ in range(5))
            want_levels = [logging.WARNING] * 5
            assert out == want_levels
