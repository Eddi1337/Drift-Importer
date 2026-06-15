from pathlib import Path

import pytest

from app.merge import MergeError, build_concat_command, write_concat_list


def test_write_concat_list_escaping(tmp_path):
    list_file = tmp_path / "list.txt"
    paths = [tmp_path / "a's clip.mp4", tmp_path / "b.mp4"]
    write_concat_list(paths, list_file)
    content = list_file.read_text()
    assert "file '" in content
    assert content.count("file '") == 2
    # single quote in filename is escaped
    assert "'\\''" in content


def test_build_concat_command(tmp_path):
    out = tmp_path / "out.mp4"
    lst = tmp_path / "l.txt"
    cmd = build_concat_command([], out, lst)
    assert "-f" in cmd and "concat" in cmd
    assert "-c" in cmd and "copy" in cmd  # stream copy, no re-encode
    assert str(out) == cmd[-1]


def test_check_compatible_requires_two():
    from app.merge import check_compatible

    with pytest.raises(MergeError):
        check_compatible([Path("only-one.mp4")])
