"""G1-C 编译期隔离与 SQ 取证合同的 import-free 静态检查。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "src/hybm/ops/hybm_kernel"
HOST = ROOT / "src/hybm/csrc/entity/hybm_sdma_orchestrator.cpp"


def test_trace_stays_out_of_mailbox_and_default_build():
    layout = (KERNEL / "hybm_g1c_device_trace.h").read_text()
    cm = (KERNEL / "CMakeLists.txt").read_text()
    assert "HYBM_G1C_DEVICE_TRACE_OFF = 0x100" in layout
    assert "sizeof(HybmG1cDeviceTrace) == 64" in layout
    assert 'MF_SDMA_ORCH_G1C_DEVICE_TRACE "Build G1-C SQ device trace; requires isolated v12 binary" OFF' in cm
    assert 'MF_SDMA_ORCH_KVER STREQUAL "12"' in cm


def test_device_samples_real_sq_head_tail_not_request_ring():
    src = (KERNEL / "hybm_sdma_orch_kernel.cc").read_text()
    assert "DRV_SQCQ_PROP_SQ_HEAD, &head" in src
    assert "DRV_SQCQ_PROP_SQ_TAIL, &tail" in src
    assert "G1C_BEFORE_DOORBELL" in src
    assert "G1C_AFTER_DOORBELL" in src
    assert "G1C_IDLE_SQ_SAMPLE" in src
    assert "#ifdef HYBM_G1C_DEVICE_TRACE" in src


def test_host_poll_is_opt_in_and_read_only():
    host = HOST.read_text()
    assert 'std::getenv("MF_SDMA_ORCH_G1C_TRACE")' in host
    assert 'std::getenv("MF_SDMA_ORCH_DIAG")' in host
    assert "HYBM_G1C_DEVICE_TRACE_OFF" in host
    assert "ACL_MEMCPY_DEVICE_TO_HOST" in host
    assert "snap.seqBegin == snap.seqEnd" in host
