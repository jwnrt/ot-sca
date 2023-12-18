#!/usr/bin/env python3
# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

# Note: The word ciphertext refers to the tag in sha3
#       To be compatible to the other capture scripts, the variable is
#       called ciphertext

import logging
import random
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from Crypto.Hash import SHA3_256
from lib.ot_communication import OTPRNG, OTSHA3, OTUART
from project_library.project import ProjectConfig, SCAProject
from scopes.cycle_converter import convert_num_cycles, convert_offset_cycles
from scopes.scope import Scope, ScopeConfig, determine_sampling_rate
from tqdm import tqdm

import util.helpers as helpers
from target.cw_fpga import CWFPGA
from util import check_version
from util import data_generator as dg
from util import plot

logger = logging.getLogger()


def abort_handler_during_loop(this_project, sig, frame):
    """ Abort capture and store traces.

    Args:
        this_project: Project instance.
    """
    if this_project is not None:
        logger.info("\nHandling keyboard interrupt")
        this_project.close(save=True)
    sys.exit(0)


@dataclass
class CaptureConfig:
    """ Configuration class for the current capture.
    """
    capture_mode: str
    batch_mode: bool
    num_traces: int
    num_segments: int
    output_len: int
    text_fixed: bytearray
    text_len_bytes: int
    protocol: str
    port: Optional[str] = "None"


def setup(cfg: dict, project: Path):
    """ Setup target, scope, and project.

    Args:
        cfg: The configuration for the current experiment.
        project: The path for the project file.

    Returns:
        The target, scope, and project.
    """
    # Calculate pll_frequency of the target.
    # target_freq = pll_frequency * target_clk_mult
    # target_clk_mult is a hardcoded constant in the FPGA bitstream.
    cfg["target"]["pll_frequency"] = cfg["target"]["target_freq"] / cfg["target"]["target_clk_mult"]

    # Init target.
    logger.info(f"Initializing target {cfg['target']['target_type']} ...")
    target = CWFPGA(
        bitstream = cfg["target"]["fpga_bitstream"],
        force_programming = cfg["target"]["force_program_bitstream"],
        firmware = cfg["target"]["fw_bin"],
        pll_frequency = cfg["target"]["pll_frequency"],
        baudrate = cfg["target"]["baudrate"],
        output_len = cfg["target"]["output_len_bytes"],
        protocol = cfg["target"]["protocol"]
    )

    # Init scope.
    scope_type = cfg["capture"]["scope_select"]

    # Determine sampling rate, if necessary.
    cfg[scope_type]["sampling_rate"] = determine_sampling_rate(cfg, scope_type)
    # Convert number of cycles into number of samples, if necessary.
    cfg[scope_type]["num_samples"] = convert_num_cycles(cfg, scope_type)
    # Convert offset in cycles into offset in samples, if necessary.
    cfg[scope_type]["offset_samples"] = convert_offset_cycles(cfg, scope_type)

    logger.info(f"Initializing scope {scope_type} with a sampling rate of {cfg[scope_type]['sampling_rate']}...")  # noqa: E501

    # Create scope config & setup scope.
    scope_cfg = ScopeConfig(
        scope_type = scope_type,
        acqu_channel = cfg[scope_type].get("channel"),
        ip = cfg[scope_type].get("waverunner_ip"),
        num_samples = cfg[scope_type]["num_samples"],
        offset_samples = cfg[scope_type]["offset_samples"],
        sampling_rate = cfg[scope_type].get("sampling_rate"),
        num_segments = cfg[scope_type]["num_segments"],
        sparsing = cfg[scope_type].get("sparsing"),
        scope_gain = cfg[scope_type].get("scope_gain"),
        pll_frequency = cfg["target"]["pll_frequency"],
    )
    scope = Scope(scope_cfg)

    # Init project.
    project_cfg = ProjectConfig(type = cfg["capture"]["trace_db"],
                                path = project,
                                wave_dtype = np.uint16,
                                overwrite = True,
                                trace_threshold = cfg["capture"].get("trace_threshold")
                                )
    project = SCAProject(project_cfg)
    project.create_project()

    return target, scope, project


def configure_cipher(cfg, target, capture_cfg) -> OTSHA3:
    """ Configure the SHA3 cipher.

    Establish communication with the SHA3 cipher and configure the seed and mask.

    Args:
        cfg: The project config.
        target: The OT target.
        capture_cfg: The capture config.

    Returns:
        The communication interface to the SHA3 cipher.
    """
    # Establish UART for uJSON command interface. Returns None for simpleserial.
    ot_uart = OTUART(protocol=capture_cfg.protocol, port=capture_cfg.port)

    # Create communication interface to OT SHA3.
    ot_sha3 = OTSHA3(target=target.target, protocol=capture_cfg.protocol,
                     port=ot_uart.uart)

    # Create communication interface to OT PRNG.
    ot_prng = OTPRNG(target=target.target, protocol=capture_cfg.protocol,
                     port=ot_uart.uart)

    if cfg["test"]["masks_off"] is True:
        logger.info("Configure device to use constant, fast entropy!")
        ot_sha3.set_mask_off()
    else:
        ot_sha3.set_mask_on()

    # If batch mode, configure PRNGs.
    if capture_cfg.batch_mode:
        # Seed host's PRNG.
        random.seed(cfg["test"]["batch_prng_seed"])

        ot_sha3.write_lfsr_seed(cfg["test"]["lfsr_seed"].to_bytes(4, "little"))
        ot_prng.seed_prng(cfg["test"]["batch_prng_seed"].to_bytes(4, "little"))

    return ot_sha3


def generate_ref_crypto(sample_fixed, mode, batch, plaintext,
                        plaintext_fixed, text_len_bytes):
    """ Generate cipher material for the encryption.

    This function derives the next key as well as the plaintext for the next
    encryption.

    Args:
        sample_fixed: Use fixed key or new key.
        mode: The mode of the capture.
        batch: Batch or non-batch mode.
        plaintext: The current plaintext.
        plaintext_fixed: The fixed plaintext for FVSR.
        text_len_bytes: Th length of the plaintext.

    Returns:
        plaintext: The next plaintext.
        ciphertext: The next ciphertext.
        sample_fixed: Is the next sample fixed or not?
    """
    if mode == "sha3_fvsr_data" and not batch:
        # returns a pt, ct, key (not used) tripple
        # does only need the sample_fixed argument
        if sample_fixed:
            # Expected ciphertext.
            plaintext, ciphertext, key = dg.get_sha3_fixed()
        else:
            plaintext, ciphertext, key = dg.get_sha3_random()
        # The next sample is either fixed or random.
        sample_fixed = plaintext[0] & 0x1
    else:
        if mode == "sha3_random":
            # returns pt, ct, needs pt as arguments
            sha3 = SHA3_256.new(bytes(plaintext))
            ciphertext_bytes = sha3.digest()
            ciphertext = [x for x in ciphertext_bytes]
        else:  # mode = sha3_fvsr_data_batch
            # returns random pt, ct, needs no arguments
            if sample_fixed:
                plaintext = plaintext_fixed
            else:
                random_plaintext = []
                for i in range(0, text_len_bytes):
                    random_plaintext.append(random.randint(0, 255))
                plaintext = random_plaintext

            # needed to be in sync with ot lfsr and for sample_fixed generation
            dummy_plaintext = []
            for i in range(0, 16):
                dummy_plaintext.append(random.randint(0, 255))
            # Compute ciphertext for this plaintext.
            sha3 = SHA3_256.new(bytes(plaintext))
            ciphertext_bytes = sha3.digest()
            ciphertext = [x for x in ciphertext_bytes]
            # Determine if next iteration uses fixed_key.
            sample_fixed = dummy_plaintext[0] & 0x1
    return plaintext, ciphertext, sample_fixed


def check_ciphertext(ot_sha3, expected_last_ciphertext, ciphertext_len):
    """ Compares the received with the generated ciphertext.

    Ciphertext is read from the device and compared against the pre-computed
    generated ciphertext. In batch mode, only the last ciphertext is compared.
    Asserts on mismatch.

    Args:
        ot_sha3: The OpenTitan SHA§ communication interface.
        expected_last_ciphertext: The pre-computed ciphertext.
        ciphertext_len: The length of the ciphertext in bytes.
    """
    actual_last_ciphertext = ot_sha3.read_ciphertext(ciphertext_len)
    assert actual_last_ciphertext == expected_last_ciphertext[0:ciphertext_len], (
        f"Incorrect encryption result!\n"
        f"actual:   {actual_last_ciphertext}\n"
        f"expected: {expected_last_ciphertext}"
    )


def capture(scope: Scope, ot_sha3: OTSHA3, capture_cfg: CaptureConfig,
            project: SCAProject, cwtarget: CWFPGA):
    """ Capture power consumption during SHA3 digest computation.

    Supports four different capture types:
    * sha3_random: random plaintext.
    * sha3_fvsr: Fixed vs. random data.
    * sha3_fvsr_batch: Fixed vs. random data batch.

    Args:
        scope: The scope class representing a scope (Husky or WaveRunner).
        ot_sha3: The OpenTitan SHA3 communication interface.
        capture_cfg: The configuration of the capture.
        project: The SCA project.
        cwtarget: The CW FPGA target.
    """
    # Initial plaintext.
    text_fixed = capture_cfg.text_fixed
    text = text_fixed

    # FVSR setup.
    # in the sha3_serial.c: `static bool run_fixed = false;`
    # we should adjust this throughout all scripts.
    sample_fixed = 0

    # Optimization for CW trace library.
    num_segments_storage = 1

    if capture_cfg.batch_mode:
        ot_sha3.fvsr_fixed_msg_set(text_fixed)

    # Register ctrl-c handler to store traces on abort.
    signal.signal(signal.SIGINT, partial(abort_handler_during_loop, project))
    # Main capture with progress bar.
    remaining_num_traces = capture_cfg.num_traces
    with tqdm(total=remaining_num_traces, desc="Capturing", ncols=80, unit=" traces") as pbar:
        while remaining_num_traces > 0:
            # Arm the scope.
            scope.arm()
            # Trigger encryption.
            if capture_cfg.batch_mode:
                # Batch mode. Is always sha3_fvsr_data
                ot_sha3.absorb_batch(
                    capture_cfg.num_segments.to_bytes(4, "little"))
            else:
                # Non batch mode. either random or fvsr
                if capture_cfg.capture_mode == "sha3_fvsr_data":
                    text, ciphertext, sample_fixed = generate_ref_crypto(
                        sample_fixed = sample_fixed,
                        mode = capture_cfg.capture_mode,
                        batch = capture_cfg.batch_mode,
                        plaintext = text,
                        plaintext_fixed = text_fixed,
                        text_len_bytes = capture_cfg.text_len_bytes
                    )
                ot_sha3.absorb(text)
            # Capture traces.
            waves = scope.capture_and_transfer_waves(cwtarget.target)
            assert waves.shape[0] == capture_cfg.num_segments

            expected_ciphertext = None
            # Generate reference crypto material and store trace.
            for i in range(capture_cfg.num_segments):
                if capture_cfg.batch_mode or capture_cfg.capture_mode == "sha3_random":
                    text, ciphertext, sample_fixed = generate_ref_crypto(
                        sample_fixed = sample_fixed,
                        mode = capture_cfg.capture_mode,
                        batch = capture_cfg.batch_mode,
                        plaintext = text,
                        plaintext_fixed = text_fixed,
                        text_len_bytes = capture_cfg.text_len_bytes
                    )
                # Sanity check retrieved data (wave).
                assert len(waves[i, :]) >= 1
                # Store trace into database.
                project.append_trace(wave = waves[i, :],
                                     plaintext = bytearray(text),
                                     ciphertext = bytearray(ciphertext),
                                     key = None)

                if capture_cfg.capture_mode == "sha3_random":
                    plaintext = bytearray(16)
                    for i in range(0, 16):
                        plaintext[i] = random.randint(0, 255)

                if capture_cfg.batch_mode:
                    exp_cipher_bytes = (ciphertext if expected_ciphertext is
                                        None else (a ^ b for (a, b) in
                                                   zip(ciphertext,
                                                       expected_ciphertext)))
                    expected_ciphertext = [x for x in exp_cipher_bytes]
                else:
                    expected_ciphertext = ciphertext

            # Compare received ciphertext with generated.
            compare_len = capture_cfg.output_len
            check_ciphertext(ot_sha3, expected_ciphertext, compare_len)

            # Memory allocation optimization for CW trace library.
            num_segments_storage = project.optimize_capture(num_segments_storage)

            # Update the loop variable and the progress bar.
            remaining_num_traces -= capture_cfg.num_segments
            pbar.update(capture_cfg.num_segments)


def print_plot(project: SCAProject, config: dict, file: Path) -> None:
    """ Print plot of traces.

    Printing the plot helps to adjust the scope gain and check for clipping.

    Args:
        project: The project containing the traces.
        config: The capture configuration.
        file: The output file path.
    """
    if config["capture"]["show_plot"]:
        plot.save_plot_to_file(project.get_waves(0, config["capture"]["plot_traces"]),
                               set_indices = None,
                               num_traces = config["capture"]["plot_traces"],
                               outfile = file,
                               add_mean_stddev=True)
        print(f'Created plot with {config["capture"]["plot_traces"]} traces: '
              f'{Path(str(file) + ".html").resolve()}')


def main(argv=None):
    # Configure the logger.
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    logger.addHandler(console)

    # Parse the provided arguments.
    args = helpers.parse_arguments(argv)

    # Check the ChipWhisperer version.
    check_version.check_cw("5.7.0")

    # Load configuration from file.
    with open(args.cfg) as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)

    # Determine the capture mode and configure the current capture.
    mode = "sha3_fvsr_data"
    batch = False
    if "sha3_random" in cfg["test"]["which_test"]:
        mode = "sha3_random"
    if "batch" in cfg["test"]["which_test"]:
        batch = True
    else:
        # For non-batch mode, make sure that num_segments = 1.
        cfg[cfg["capture"]["scope_select"]]["num_segments"] = 1
        logger.info("num_segments needs to be 1 in non-batch mode. Setting num_segments=1.")

    # Setup the target, scope and project.
    target, scope, project = setup(cfg, args.project)

    # Create capture config object.
    capture_cfg = CaptureConfig(capture_mode = mode,
                                batch_mode = batch,
                                num_traces = cfg["capture"]["num_traces"],
                                num_segments = cfg[cfg["capture"]["scope_select"]]["num_segments"],
                                output_len = cfg["target"]["output_len_bytes"],
                                text_fixed = cfg["test"]["text_fixed"],
                                text_len_bytes = cfg["test"]["text_len_bytes"],
                                protocol = cfg["target"]["protocol"],
                                port = cfg["target"].get("port"))
    logger.info(f"Setting up capture {capture_cfg.capture_mode} batch={capture_cfg.batch_mode}...")

    # Configure cipher.
    ot_sha3 = configure_cipher(cfg, target, capture_cfg)

    # Capture traces.
    capture(scope, ot_sha3, capture_cfg, project, target)

    # Print plot.
    print_plot(project, cfg, args.project)

    # Save metadata.
    metadata = {}
    metadata["datetime"] = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
    metadata["cfg"] = cfg
    metadata["num_samples"] = scope.scope_cfg.num_samples
    metadata["offset_samples"] = scope.scope_cfg.offset_samples
    metadata["scope_gain"] = scope.scope_cfg.scope_gain
    metadata["cfg_file"] = str(args.cfg)
    metadata["fpga_bitstream"] = cfg["target"]["fpga_bitstream"]
    # TODO: Store binary into database instead of binary path.
    # (Issue lowrisc/ot-sca#214)
    metadata["fw_bin"] = cfg["target"]["fw_bin"]
    metadata["notes"] = args.notes
    project.write_metadata(metadata)

    # Save and close project.
    project.save()


if __name__ == "__main__":
    main()