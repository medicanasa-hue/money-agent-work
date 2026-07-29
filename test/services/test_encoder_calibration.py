import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoAspect
from app.services import encoder_calibration


class TestEncoderCalibration(unittest.TestCase):
    def test_build_amf_calibration_plan_uses_selected_output_resolution(self):
        plan = encoder_calibration.build_amf_calibration_plan(
            VideoAspect.portrait_4_5,
            baseline_qp_i=12,
        )

        self.assertEqual(plan["video_aspect"], "4:5")
        self.assertEqual(plan["resolution"], [1080, 1350])
        self.assertEqual(
            [candidate["qp_i"] for candidate in plan["candidates"]],
            [10, 12, 14],
        )

    def test_parse_measurement_output_reads_ssim_and_psnr(self):
        metrics = encoder_calibration._parse_measurement_output(
            "[Parsed_ssim_0] SSIM Y:0.995 U:0.998 V:0.998 All:0.996123\n"
            "[Parsed_psnr_1] PSNR y:40.2 u:42.1 v:42.3 average:41.123456\n"
        )

        self.assertEqual(metrics, {"ssim": 0.996123, "psnr": 41.123456})

    def test_run_amf_calibration_skips_when_amf_is_not_configured(self):
        with patch.object(
            encoder_calibration.video,
            "_get_configured_video_codec",
            return_value="libx264",
        ):
            result = encoder_calibration.run_amf_calibration()

        self.assertEqual(result["status"], "not_amf_configured")
        self.assertFalse(result["ok"])
        self.assertEqual(result["configured_codec"], "libx264")
