import io
import tarfile

import pytest

from immich_age_guesser.estimators.mivolo_install import PATCHES, install

ORIGINAL = """from timm.models._helpers import load_state_dict, remap_checkpoint
from timm.models._pretrained import PretrainedCfg, split_model_name_tag

def create_model(model_name):
    pretrained_cfg, model_name = load_model_config_from_hf(model_name)
"""


def archive(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"MiVOLO-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_installs_only_the_package_and_patches_it(tmp_path):
    data = archive({"mivolo/__init__.py": "", "mivolo/model/create_timm_model.py": ORIGINAL,
                    "setup.py": "import pkg_resources", "demo.py": "x"})
    dest = install(tmp_path, archive=data)
    assert dest == tmp_path / "mivolo"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mivolo"]
    text = (dest / "model/create_timm_model.py").read_text()
    for _, _, new in PATCHES:
        assert new in text
    compile(text, "create_timm_model.py", "exec")
    # Re-installing replaces the previous copy.
    install(tmp_path, archive=data)


def test_fails_loudly_when_upstream_changed(tmp_path):
    data = archive({"mivolo/model/create_timm_model.py": "from timm import something_else\n"})
    with pytest.raises(RuntimeError, match="no longer applies"):
        install(tmp_path, archive=data)
    assert not (tmp_path / "mivolo").exists()
