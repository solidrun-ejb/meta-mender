#!/usr/bin/python
# Copyright 2017 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from fabric.api import *

import json
import os
import pytest
import re
import shutil
import subprocess
import tempfile

# Make sure common is imported after fabric, because we override some functions.
from common import *


class Helpers:
    @staticmethod
    def upload_to_s3(artifact):
        subprocess.call(["s3cmd", "--follow-symlinks", "put", artifact, "s3://mender/temp/"])
        subprocess.call(["s3cmd", "setacl", "s3://mender/temp/%s" % artifact, "--acl-public"])

    @staticmethod
    # TODO: Use this when mender is more stable. Spurious errors are currently generated.
    def check_journal_errors():
        output = run("journalctl -a -u mender | grep error")
        assert output == 1

    @staticmethod
    def get_env_offsets(bitbake_variables):
        offsets = [0, 0]

        alignment = int(bitbake_variables["MENDER_PARTITION_ALIGNMENT"])
        env_size = os.stat(os.path.join(bitbake_variables["DEPLOY_DIR_IMAGE"], "uboot.env")).st_size
        offsets[0] = int(bitbake_variables["MENDER_UBOOT_ENV_STORAGE_DEVICE_OFFSET"])
        offsets[1] = offsets[0] + int(env_size / 2)

        assert(offsets[0] % alignment == 0)
        assert(offsets[1] % alignment == 0)

        return offsets

    @staticmethod
    def get_env_checksums(bitbake_variables):
        checksums = [0, 0]

        offsets = Helpers.get_env_offsets(bitbake_variables)
        dev = bitbake_variables["MENDER_STORAGE_DEVICE"]

        run("dd if=%s of=/data/env1.tmp bs=1 count=4 skip=%d" % (dev, offsets[0]))
        run("dd if=%s of=/data/env2.tmp bs=1 count=4 skip=%d" % (dev, offsets[1]))

        get("env1.tmp", remote_path="/data")
        get("env2.tmp", remote_path="/data")
        run("rm -f /data/env1.tmp /data/env2.tmp")

        env = open("env1.tmp")
        checksums[0] = env.read()
        env.close()
        env = open("env2.tmp")
        checksums[1] = env.read()
        env.close()

        os.remove("env1.tmp")
        os.remove("env2.tmp")

        return checksums

    @staticmethod
    def corrupt_middle_byte(fd):
        # Corrupt the middle byte in the contents.
        middle = int(os.fstat(fd.fileno()).st_size / 2)
        fd.seek(middle)
        middle_byte = int(fd.read(1).encode("hex"), base=16)
        fd.seek(middle)
        # Flip lowest bit.
        fd.write("%c" % (middle_byte ^ 0x1))

    @staticmethod
    def get_file_flag(bitbake_variables):
        if version_is_minimum(bitbake_variables, "mender-artifact", "3.0.0"):
            return "-f"
        else:
            return "-u"

    @staticmethod
    def get_install_flag():
        with settings(warn_only=True):
            output = run("mender --help 2>&1")
            if re.search("^\s*install(\s|$)", output, flags=re.MULTILINE):
                return "install"
            elif re.search("^\s*-install(\s|$)", output, flags=re.MULTILINE):
                return "-install"
            else:
                return "-rootfs"

    @staticmethod
    # Note: `image` needs to be in current directory.
    def install_update(image):
        http_server_location = pytest.config.getoption("--http-server")
        use_s3 = pytest.config.getoption("--use-s3")
        board = pytest.config.getoption("--board-type")
        install_flag = Helpers.get_install_flag()

        http_server = None
        if "qemu" not in board or use_s3:
            Helpers.upload_to_s3(image)
            s3_address = pytest.config.getoption("--s3-address")
            http_server_location = "{}/mender/temp".format(s3_address)
        else:
            http_server = subprocess.Popen(["python", "-m", "SimpleHTTPServer"])
            assert(http_server)

        try:
            output = run("mender %s http://%s/%s" % (install_flag, http_server_location, image))
            print("output from rootfs update: ", output)
        finally:
            if http_server:
                http_server.terminate()


class SignatureCase:
    label = ""
    signature = False
    signature_ok = False
    key = False
    key_type = ""
    checksum_ok = True
    header_checksum_ok = True

    update_written = False
    success = True

    def __init__(self,
                 label,
                 signature,
                 signature_ok,
                 key,
                 key_type,
                 checksum_ok,
                 header_checksum_ok,
                 update_written,
                 artifact_version,
                 success):
        self.label = label
        self.signature = signature
        self.signature_ok = signature_ok
        self.key = key
        self.key_type = key_type
        self.checksum_ok = checksum_ok
        self.header_checksum_ok = header_checksum_ok
        self.update_written = update_written
        self.artifact_version = artifact_version
        self.success = success

@pytest.mark.usefixtures("no_image_file", "setup_board", "bitbake_path")
class TestUpdates:

    @pytest.mark.min_mender_version('1.0.0')
    def test_broken_image_update(self, bitbake_variables):

        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_broken_image_update, bitbake_variables)
            return

        file_flag = Helpers.get_file_flag(bitbake_variables)
        install_flag = Helpers.get_install_flag()
        (active_before, passive_before) = determine_active_passive_part(bitbake_variables)

        image_type = bitbake_variables["MENDER_DEVICE_TYPE"]

        try:
            # Make a dummy/broken update
            subprocess.call("dd if=/dev/zero of=image.dat bs=1M count=0 seek=16", shell=True)
            subprocess.call("mender-artifact write rootfs-image -t %s -n test-update %s image.dat -o image.mender"
                            % (image_type, file_flag), shell=True)
            put("image.mender", remote_path="/var/tmp/image.mender")
            run("mender %s /var/tmp/image.mender" % install_flag)
            reboot()

            # Now qemu is auto-rebooted twice; once to boot the dummy image,
            # where it fails, and uboot auto-reboots a second time into the
            # original partition.

            output = run_after_connect("mount")

            # The update should have reverted to the original active partition,
            # since the image was bogus.
            assert(output.find(active_before) >= 0)
            assert(output.find(passive_before) < 0)

        finally:
            # Cleanup.
            os.remove("image.mender")
            os.remove("image.dat")

    @pytest.mark.min_mender_version('1.0.0')
    def test_too_big_image_update(self, bitbake_variables):
        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_too_big_image_update, bitbake_variables)
            return

        file_flag = Helpers.get_file_flag(bitbake_variables)
        install_flag = Helpers.get_install_flag()
        image_type = bitbake_variables["MENDER_DEVICE_TYPE"]

        try:
            # Make a too big update
            subprocess.call("dd if=/dev/zero of=image.dat bs=1M count=0 seek=1024", shell=True)
            subprocess.call("mender-artifact write rootfs-image -t %s -n test-update-too-big %s image.dat -o image-too-big.mender"
                            % (image_type, file_flag), shell=True)
            put("image-too-big.mender", remote_path="/var/tmp/image-too-big.mender")
            output = run("mender %s /var/tmp/image-too-big.mender ; echo 'ret_code=$?'" % install_flag)

            assert(output.find("no space left on device") >= 0)
            assert(output.find("ret_code=0") < 0)

        finally:
            # Cleanup.
            os.remove("image-too-big.mender")
            os.remove("image.dat")

    @pytest.mark.min_mender_version('1.0.0')
    def test_network_based_image_update(self, successful_image_update_mender, bitbake_variables):
        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_network_based_image_update, successful_image_update_mender, bitbake_variables)
            return

        (active_before, passive_before) = determine_active_passive_part(bitbake_variables)

        Helpers.install_update(successful_image_update_mender)

        output = run("fw_printenv bootcount")
        assert(output == "bootcount=0")

        output = run("fw_printenv upgrade_available")
        assert(output == "upgrade_available=1")

        output = run("fw_printenv mender_boot_part")
        assert(output == "mender_boot_part=" + passive_before[-1:])

        # Delete kernel and associated files from currently running partition,
        # so that the boot will fail if U-Boot for any reason tries to grab the
        # kernel from the wrong place.
        run("rm -f /boot/* || true")

        reboot()

        run_after_connect("true")
        (active_after, passive_after) = determine_active_passive_part(bitbake_variables)

        # The OS should have moved to a new partition, since the image was fine.
        assert(active_after == passive_before)
        assert(passive_after == active_before)

        output = run("fw_printenv bootcount")
        assert(output == "bootcount=1")

        output = run("fw_printenv upgrade_available")
        assert(output == "upgrade_available=1")

        output = run("fw_printenv mender_boot_part")
        assert(output == "mender_boot_part=" + active_after[-1:])

        run("mender -commit")

        output = run("fw_printenv upgrade_available")
        assert(output == "upgrade_available=0")

        output = run("fw_printenv mender_boot_part")
        assert(output == "mender_boot_part=" + active_after[-1:])

        active_before = active_after
        passive_before = passive_after

        reboot()

        run_after_connect("true")
        (active_after, passive_after) = determine_active_passive_part(bitbake_variables)

        # The OS should have stayed on the same partition, since we committed.
        assert(active_after == active_before)
        assert(passive_after == passive_before)

    @pytest.mark.parametrize("sig_case",
                             [SignatureCase(label="Not signed, key not present",
                                            signature=False,
                                            signature_ok=False,
                                            key=False,
                                            key_type=None,
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=True),
                              SignatureCase(label="RSA, Correctly signed, key present",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=True),
                              SignatureCase(label="RSA, Incorrectly signed, key present",
                                            signature=True,
                                            signature_ok=False,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="RSA, Correctly signed, key not present",
                                            signature=True,
                                            signature_ok=True,
                                            key=False,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=True),
                              SignatureCase(label="RSA, Not signed, key present",
                                            signature=False,
                                            signature_ok=False,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="RSA, Correctly signed, but checksum wrong, key present",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=False,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, key present",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=True),
                              SignatureCase(label="EC, Incorrectly signed, key present",
                                            signature=True,
                                            signature_ok=False,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, key not present",
                                            signature=True,
                                            signature_ok=True,
                                            key=False,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=True),
                              SignatureCase(label="EC, Not signed, key present",
                                            signature=False,
                                            signature_ok=False,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, but checksum wrong, key present",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=False,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=None,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, but header does not match checksum, key present",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=False,
                                            update_written=False,
                                            artifact_version=None,
                                            success=False),

                              SignatureCase(label="Not signed, key not present, version 2",
                                            signature=False,
                                            signature_ok=False,
                                            key=False,
                                            key_type=None,
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=True),
                              SignatureCase(label="RSA, Correctly signed, key present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=True),
                              SignatureCase(label="RSA, Incorrectly signed, key present, version 2",
                                            signature=True,
                                            signature_ok=False,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="RSA, Correctly signed, key not present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=False,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=True),
                              SignatureCase(label="RSA, Not signed, key present, version 2",
                                            signature=False,
                                            signature_ok=False,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="RSA, Correctly signed, but checksum wrong, key present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="RSA",
                                            checksum_ok=False,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, key present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=True),
                              SignatureCase(label="EC, Incorrectly signed, key present, version 2",
                                            signature=True,
                                            signature_ok=False,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, key not present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=False,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=True),
                              SignatureCase(label="EC, Not signed, key present, version 2",
                                            signature=False,
                                            signature_ok=False,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=True,
                                            update_written=False,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, but checksum wrong, key present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=False,
                                            header_checksum_ok=True,
                                            update_written=True,
                                            artifact_version=2,
                                            success=False),
                              SignatureCase(label="EC, Correctly signed, but header does not match checksum, key present, version 2",
                                            signature=True,
                                            signature_ok=True,
                                            key=True,
                                            key_type="EC",
                                            checksum_ok=True,
                                            header_checksum_ok=False,
                                            update_written=False,
                                            artifact_version=2,
                                            success=False),
                             ])
    @pytest.mark.min_mender_version('1.1.0')
    def test_signed_updates(self, sig_case, bitbake_path, bitbake_variables):
        """Test various combinations of signed and unsigned, present and non-
        present verification keys."""

        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_signed_updates, sig_case, bitbake_path, bitbake_variables)
            return

        file_flag = Helpers.get_file_flag(bitbake_variables)
        install_flag = Helpers.get_install_flag()

        # mmc mount points are named: /dev/mmcblk0p1
        # ubi volumes are named: ubi0_1
        (active, passive) = determine_active_passive_part(bitbake_variables)
        if passive.startswith('ubi'):
            passive = '/dev/' + passive

        # Generate "update" appropriate for this test case.
        # Cheat a little. Instead of spending a lot of time on a lot of reboots,
        # just verify that the contents of the update are correct.
        new_content = sig_case.label
        with open("image.dat", "w") as fd:
            fd.write(new_content)
            # Write some extra data just to make sure the update is big enough
            # to be written even if the checksum is wrong. If it's too small it
            # may fail before it has a chance to be written.
            fd.write("\x00" * (1048576 * 8))

        artifact_args = ""

        # Generate artifact with or without signature.
        if sig_case.signature:
            artifact_args += " -k %s" % signing_key(sig_case.key_type).private

        # Generate artifact with specific version. None means default.
        if sig_case.artifact_version is not None:
            artifact_args += " -v %d" % sig_case.artifact_version

        if sig_case.key_type:
            sig_key = signing_key(sig_case.key_type)
        else:
            sig_key = None

        image_type = bitbake_variables["MENDER_DEVICE_TYPE"]

        subprocess.check_call("mender-artifact write rootfs-image %s -t %s -n test-update %s image.dat -o image.mender"
                              % (artifact_args, image_type, file_flag), shell=True)

        # If instructed to, corrupt the signature and/or checksum.
        if (sig_case.signature and not sig_case.signature_ok) or not sig_case.checksum_ok or not sig_case.header_checksum_ok:
            tar = subprocess.check_output(["tar", "tf", "image.mender"])
            tar_list = tar.split()
            tmpdir = tempfile.mkdtemp()
            try:
                shutil.copy("image.mender", os.path.join(tmpdir, "image.mender"))
                cwd = os.open(".", os.O_RDONLY)
                os.chdir(tmpdir)
                try:
                    tar = subprocess.check_output(["tar", "xf", "image.mender"])
                    if not sig_case.signature_ok:
                        # Corrupt signature.
                        with open("manifest.sig", "r+") as fd:
                            Helpers.corrupt_middle_byte(fd)
                    if not sig_case.checksum_ok:
                        os.chdir("data")
                        try:
                            data_list = subprocess.check_output(["tar", "tzf", "0000.tar.gz"])
                            data_list = data_list.split()
                            subprocess.check_call(["tar", "xzf", "0000.tar.gz"])
                            # Corrupt checksum by changing file slightly.
                            with open("image.dat", "r+") as fd:
                                Helpers.corrupt_middle_byte(fd)
                            # Pack it up again in same order.
                            os.remove("0000.tar.gz")
                            subprocess.check_call(["tar", "czf", "0000.tar.gz"] + data_list)
                            for data_file in data_list:
                                os.remove(data_file)
                        finally:
                            os.chdir("..")

                    if not sig_case.header_checksum_ok:
                        data_list = subprocess.check_output(["tar", "tzf", "header.tar.gz"])
                        data_list = data_list.split()
                        subprocess.check_call(["tar", "xzf", "header.tar.gz"])
                        # Corrupt checksum by changing file slightly.
                        with open("headers/0000/files", "a") as fd:
                            # Some extra data to corrupt the header checksum,
                            # but still valid JSON.
                            fd.write(" ")
                        # Pack it up again in same order.
                        os.remove("header.tar.gz")
                        subprocess.check_call(["tar", "czf", "header.tar.gz"] + data_list)
                        for data_file in data_list:
                            os.remove(data_file)

                    # Make sure we put it back in the same order.
                    os.remove("image.mender")
                    subprocess.check_call(["tar", "cf", "image.mender"] + tar_list)
                finally:
                    os.fchdir(cwd)
                    os.close(cwd)

                shutil.move(os.path.join(tmpdir, "image.mender"), "image.mender")

            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        put("image.mender", remote_path="/data/")
        try:
            # Update key configuration on device.
            run("cp /etc/mender/mender.conf /data/etc/mender/mender.conf.bak")
            get("mender.conf", remote_path="/etc/mender")
            with open("mender.conf") as fd:
                config = json.load(fd)
            if sig_case.key:
                config['ArtifactVerifyKey'] = "/data/etc/mender/%s" % os.path.basename(sig_key.public)
                put(sig_key.public, remote_path="/data/etc/mender")
            else:
                if config.get('ArtifactVerifyKey'):
                    del config['ArtifactVerifyKey']
            with open("mender.conf", "w") as fd:
                json.dump(config, fd)
            put("mender.conf", remote_path="/etc/mender")
            os.remove("mender.conf")

            # Start by writing known "old" content in the partition.
            old_content = "Preexisting partition content"
            if 'ubi' in passive:
                # ubi volumes cannot be directly written to, we have to use
                # ubiupdatevol
                run('echo "%s" | dd of=/tmp/update.tmp && ' \
                    'ubiupdatevol %s /tmp/update.tmp; ' \
                    'rm -f /tmp/update.tmp' % (old_content, passive))
            else:
                run('echo "%s" | dd of=%s' % (old_content, passive))

            with settings(warn_only=True):
                result = run("mender %s /data/image.mender" % install_flag)

            if sig_case.success:
                if result.return_code != 0:
                    pytest.fail("Update failed when it should have succeeded: %s, Output: %s" % (sig_case.label, result))
            else:
                if result.return_code == 0:
                    pytest.fail("Update succeeded when it should not have: %s, Output: %s" % (sig_case.label, result))

            if sig_case.update_written:
                expected_content = new_content
            else:
                expected_content = old_content

            try:
                content = run("dd if=%s bs=%d count=1"
                              % (passive, len(expected_content)))
                assert content == expected_content, "Case: %s" % sig_case.label

            # In Fabric context, SystemExit means CalledProcessError. We should
            # not catch all exceptions, because we want to leave assertions
            # alone.
            except SystemExit:
                if "mender-ubi" in bitbake_variables['DISTRO_FEATURES'].split():
                    # For UBI volumes specifically: The UBI_IOCVOLUP call which
                    # Mender uses prior to writing the data, takes a size
                    # argument, and if you don't write that amount of bytes, the
                    # volume is marked corrupted as a security measure. This
                    # sometimes triggers in our checksum mismatch tests, so
                    # accept the volume being unreadable in that case.
                    pass
                else:
                    raise

        finally:
            # Reset environment to what it was.
            run("fw_setenv mender_boot_part %s" % active[-1:])
            run("fw_setenv mender_boot_part_hex %x" % int(active[-1:]))
            run("fw_setenv upgrade_available 0")
            run("cp -L /data/etc/mender/mender.conf.bak $(realpath /etc/mender/mender.conf)")
            if sig_key:
                run("rm -f /etc/mender/%s" % os.path.basename(sig_key.public))


    @pytest.mark.only_for_machine('vexpress-qemu')
    @pytest.mark.only_with_distro_feature('mender-uboot')
    @pytest.mark.min_mender_version('1.0.0')
    def test_redundant_uboot_env(self, successful_image_update_mender, bitbake_variables):
        """This tests a very specific scenario: Consider the following production
        scenario: You are currently running an update on rootfs partition
        B. Then you attempt another update, which happens to be broken (but you
        don't know that yet). This will put the update in rootfs partition
        A. However, just as U-Boot is about to switch to rootfs partition A,
        using `upgrade_available=1` (and hence triggering bootlimit), the device
        loses power. This causes the stored U-Boot environment to become
        corrupt. On the next boot, U-Boot detects this and reverts to its built
        in environment instead.

        But this is a problem: The default environment will boot from rootfs
        partition A, which contains a broken update. And since U-Boot at this
        point doesn't know that an update was in progress, it will not attempt
        to boot from anywhere else (`upgrade_available=0`). Hence the device is
        bricked.

        This is what a redundant U-Boot environment is supposed to protect
        against by always providing two copies of the stored environment, and
        guaranteeing that at least one of them is always valid.

        In a test we cannot pull the power from the device reliably, but it's
        quite easy to simulate the situation by setting up the above scenario,
        and then corrupting the environment manually with a file write.

        """

        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_redundant_uboot_env, successful_image_update_mender, bitbake_variables)
            return

        (active, passive) = determine_active_passive_part(bitbake_variables)

        if active != bitbake_variables["MENDER_ROOTFS_PART_B"]:
            # We are not running the secondary partition. This is a requirement
            # for this test to test the correct scenario. Do a full update, so
            # that we end up on the right partition. Run the full update test to
            # correct this. If running all the tests in order with a fresh
            # build, the correct partition will usually be selected already.
            self.test_network_based_image_update(successful_image_update_mender, bitbake_variables)

            (active, passive) = determine_active_passive_part(bitbake_variables)
            assert(active == bitbake_variables["MENDER_ROOTFS_PART_B"])

        file_flag = Helpers.get_file_flag(bitbake_variables)
        install_flag = Helpers.get_install_flag()

        # Make a note of the checksums of each environment. We use this later to
        # determine which one changed.
        old_checksums = Helpers.get_env_checksums(bitbake_variables)

        orig_env = run("fw_printenv")

        image_type = bitbake_variables["MENDER_DEVICE_TYPE"]

        try:
            # Make a dummy/broken update
            subprocess.call("dd if=/dev/zero of=image.dat bs=1M count=0 seek=8", shell=True)
            subprocess.call("mender-artifact write rootfs-image -t %s -n test-update %s image.dat -o image.mender"
                            % (image_type, file_flag), shell=True)
            put("image.mender", remote_path="/var/tmp/image.mender")
            run("mender %s /var/tmp/image.mender" % install_flag)

            new_checksums = Helpers.get_env_checksums(bitbake_variables)

            # Exactly one checksum should be different.
            assert(old_checksums[0] == new_checksums[0] or old_checksums[1] == new_checksums[1])
            assert(old_checksums[0] != new_checksums[0] or old_checksums[1] != new_checksums[1])

            if old_checksums[0] != new_checksums[0]:
                to_corrupt = 0
            elif old_checksums[1] != new_checksums[1]:
                to_corrupt = 1

            offsets = Helpers.get_env_offsets(bitbake_variables)

            # Now manually corrupt the environment.
            # A few bytes should do it!
            run("dd if=/dev/zero of=%s bs=1 count=64 seek=%d"
                % (bitbake_variables["MENDER_STORAGE_DEVICE"], offsets[to_corrupt]))
            run("sync")

            # Check atomicity of Mender environment update: The contents of the
            # environment before the update should be identical to the
            # environment we get if we update, and then corrupt the new
            # environment. If it's not identical, it's an indication that there
            # were intermediary steps. This is important to avoid so that the
            # environment is not in a half updated state.
            new_env = run("fw_printenv")
            assert orig_env == new_env

            reboot()

            # We should have recovered.
            run_after_connect("true")

            # And we should be back at the second rootfs partition.
            (active, passive) = determine_active_passive_part(bitbake_variables)
            assert(active == bitbake_variables["MENDER_ROOTFS_PART_B"])

        finally:
            # Cleanup.
            os.remove("image.mender")
            os.remove("image.dat")

    @pytest.mark.only_with_distro_feature('mender-grub')
    @pytest.mark.min_mender_version('1.0.0')
    def test_redundant_grub_env(self, successful_image_update_mender, bitbake_variables):
        """This tests pretty much the same thing as the test_redundant_uboot_env
        above, but the details differ. U-Boot maintains a counter in each
        environment, and then only updates one of them. However, the GRUB
        variant we have implemented in the GRUB scripting language, where we
        cannot do this, so instead we update both, and use the validity of the
        variables instead as a crude checksum."""

        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_redundant_grub_env, successful_image_update_mender, bitbake_variables)
            return

        (active, passive) = determine_active_passive_part(bitbake_variables)

        # Corrupt the passive partition.
        run("dd if=/dev/zero of=%s bs=1024 count=1024" % passive)

        if "mender-bios" in bitbake_variables['DISTRO_FEATURES'].split():
            env_dir = "/boot/grub"
        else:
            env_dir = "/boot/efi/EFI/BOOT"

        # Now try to corrupt the environment, and make sure it doesn't get booted into.
        for env_num in [1, 2]:
            # Make a copy of the two environments.
            run("cp %s/{mender_grubenv1/env,mender_grubenv1/env.backup}" % env_dir)
            run("cp %s/{mender_grubenv1/lock,mender_grubenv1/lock.backup}" % env_dir)
            run("cp %s/{mender_grubenv2/env,mender_grubenv2/env.backup}" % env_dir)
            run("cp %s/{mender_grubenv2/lock,mender_grubenv2/lock.backup}" % env_dir)

            try:
                env_file = "%s/mender_grubenv%d/env" % (env_dir, env_num)
                lock_file = "%s/mender_grubenv%d/lock" % (env_dir, env_num)
                run('sed -e "s/editing=.*/editing=1/" %s' % lock_file)
                run('sed -e "s/mender_boot_part=.*/mender_boot_part=%s/" %s' % (passive[-1], lock_file))

                reboot()
                run_after_connect("true")

                (new_active, new_passive) = determine_active_passive_part(bitbake_variables)
                assert new_active == active
                assert new_passive == passive

            finally:
                # Restore the two environments.
                run("mv %s/{mender_grubenv1/env.backup,mender_grubenv1/env}" % env_dir)
                run("mv %s/{mender_grubenv1/lock.backup,mender_grubenv1/lock}" % env_dir)
                run("mv %s/{mender_grubenv2/env.backup,mender_grubenv2/env}" % env_dir)
                run("mv %s/{mender_grubenv2/lock.backup,mender_grubenv2/lock}" % env_dir)

    @pytest.mark.only_with_distro_feature('mender-uboot')
    @pytest.mark.only_with_image('sdimg', 'uefiimg')
    @pytest.mark.min_mender_version('1.6.0')
    def test_uboot_mender_saveenv_canary(self, bitbake_variables):
        """Tests that the mender_saveenv_canary works correctly, which tests
        that Mender will not proceed unless the U-Boot boot loader has saved the
        environment."""

        if not env.host_string:
            # This means we are not inside execute(). Recurse into it!
            execute(self.test_uboot_mender_saveenv_canary, bitbake_variables)
            return

        file_flag = Helpers.get_file_flag(bitbake_variables)
        install_flag = Helpers.get_install_flag()
        image_type = bitbake_variables["MACHINE"]

        try:
            # Make a dummy/broken update
            subprocess.call("dd if=/dev/zero of=image.dat bs=1M count=0 seek=16", shell=True)
            subprocess.call("mender-artifact write rootfs-image -t %s -n test-update %s image.dat -o image.mender"
                            % (image_type, file_flag), shell=True)
            put("image.mender", remote_path="/var/tmp/image.mender")

            # Zero the environment, causing the fw-utils to use their built in
            # default.
            env_conf = run("cat /etc/fw_env.config")
            env_conf_lines = env_conf.split('\n')
            assert len(env_conf_lines) == 2
            for i in [0, 1]:
                entry = env_conf_lines[i].split()
                run("dd if=%s skip=%d bs=%d count=1 iflag=skip_bytes > /data/old_env%d"
                    % (entry[0], int(entry[1], 0), int(entry[2], 0), i))
                run("dd if=/dev/zero of=%s seek=%d bs=%d count=1 oflag=seek_bytes"
                    % (entry[0], int(entry[1], 0), int(entry[2], 0)))

            try:
                output = run("mender %s /var/tmp/image.mender", install_flag)
                pytest.fail("Update succeeded when canary was not present!")
            except:
                output = run("fw_printenv upgrade_available")
                # Upgrade should not have been triggered.
                assert(output == "upgrade_available=0")
            finally:
                # Restore environment to what it was.
                for i in [0, 1]:
                    entry = env_conf_lines[i].split()
                    run("dd of=%s seek=%d bs=%d count=1 oflag=seek_bytes < /data/old_env%d"
                        % (entry[0], int(entry[1], 0), int(entry[2], 0), i))
                    run("rm -f /data/old_env%d" % i)

        finally:
            # Cleanup.
            os.remove("image.mender")
            os.remove("image.dat")
