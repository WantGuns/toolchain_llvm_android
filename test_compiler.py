#!/usr/bin/env python
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pylint: disable=not-callable, relative-import

import argparse
import multiprocessing
import os
import shutil
import subprocess

import build
import compiler_wrapper
import utils

TARGETS = ('aosp_angler-eng', 'aosp_bullhead-eng', 'aosp_marlin-eng')
DEFAULT_TIDY_CHECKS = ('*', '-readability-*', '-google-readability-*',
                       '-google-runtime-references', '-cppcoreguidelines-*',
                       '-modernize-*', '-clang-analyzer-alpha*')

# We may introduce some new warnings after rebasing and we need to disable them
# before we fix those warnings.
DISABLED_WARNINGS = [
    '-Wno-error=defaulted-function-deleted',
    '-Wno-error=string-plus-int',
    '-fsplit-lto-unit',
]


class ClangProfileHandler(object):

    def __init__(self):
        self.profiles_dir = utils.out_path('clang-profiles')
        self.profiles_format = os.path.join(self.profiles_dir, '%4m.profraw')

    def getProfileFileEnvVar(self):
        return ('LLVM_PROFILE_FILE', self.profiles_format)

    def mergeProfiles(self):
        stage1_install = utils.out_path('stage1-install')
        profdata = os.path.join(stage1_install, 'bin', 'llvm-profdata')

        profdata_file = build.pgo_profdata_filename()

        dist_dir = os.environ.get('DIST_DIR', utils.out_path())
        out_file = os.path.join(dist_dir, profdata_file)

        cmd = [profdata, 'merge', '-o', out_file, self.profiles_dir]
        subprocess.check_call(cmd)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('android_path', help='Android source directory.')
    parser.add_argument(
        '--clang-path',
        nargs='?',
        help='Directory with a previously built Clang.')
    parser.add_argument(
        '--clang-package-path',
        nargs='?',
        help='Directory of a pre-packaged (.tar.bz2) Clang. '
        'Toolchain extracted from the package will be used.')
    parser.add_argument(
        '-k',
        '--keep-going',
        action='store_true',
        default=False,
        help='Keep going when some targets '
        'cannot be built.')
    parser.add_argument(
        '-j',
        action='store',
        dest='jobs',
        type=int,
        default=multiprocessing.cpu_count(),
        help='Number of executed jobs.')
    parser.add_argument(
        '--build-only',
        action='store_true',
        default=False,
        help='Build default targets only.')
    parser.add_argument(
        '--flashall-path',
        nargs='?',
        help='Use internal '
        'flashall tool if the path is set.')
    parser.add_argument(
        '-t',
        '--target',
        nargs='?',
        help='Build for specified '
        'target. This will work only when --build-only is '
        'enabled.')
    parser.add_argument(
        '--with-tidy',
        action='store_true',
        default=False,
        help='Enable clang tidy for Android build.')
    clean_built_target_group = parser.add_mutually_exclusive_group()
    clean_built_target_group.add_argument(
        '--clean-built-target',
        action='store_true',
        default=True,
        help='Clean output for each target that is built.')
    clean_built_target_group.add_argument(
        '--no-clean-built-target',
        action='store_false',
        dest='clean_built_target',
        help='Do not remove target output.')
    redirect_stderr_group = parser.add_mutually_exclusive_group()
    redirect_stderr_group.add_argument(
        '--redirect-stderr',
        action='store_true',
        default=True,
        help='Redirect clang stderr to $OUT/clang-error.log.')
    redirect_stderr_group.add_argument(
        '--no-redirect-stderr',
        action='store_false',
        dest='redirect_stderr',
        help='Do not redirect clang stderr.')

    parser.add_argument(
        '--generate-clang-profile',
        action='store_true',
        default=False,
        dest='profile',
        help='Build instrumented compiler and gather profiles')

    parser.add_argument(
        '--no-pgo',
        action='store_true',
        default=False,
        help='Do not use PGO profile to build stage2 Clang (defaults to False)')

    args = parser.parse_args()
    if args.profile and not args.no_pgo:
        parser.error(
            '--no-pgo must be specified along with --generate-clang-profile')
    if args.clang_path and args.clang_package_path:
        parser.error('Only one of --clang-path and --clang-package-path must'
                     'be specified')

    return args


def link_clang(android_base, clang_path):
    android_clang_path = os.path.join(android_base, 'prebuilts',
                                      'clang', 'host',
                                      utils.build_os_type(), 'clang-dev')
    utils.remove(android_clang_path)
    os.symlink(os.path.abspath(clang_path), android_clang_path)


def get_connected_device_list():
    try:
        # Get current connected device list.
        out = subprocess.check_output(['adb', 'devices', '-l'])
        devices = [x.split() for x in out.strip().split('\n')[1:]]
        return devices
    except subprocess.CalledProcessError:
        # If adb is not working properly. Return empty list.
        return []


def rm_current_product_out():
    if 'ANDROID_PRODUCT_OUT' in os.environ:
        utils.remove(os.environ['ANDROID_PRODUCT_OUT'])


def build_target(android_base, clang_version, target, max_jobs, redirect_stderr,
                 with_tidy, profiler=None):
    jobs = '-j{}'.format(max(1, min(max_jobs, multiprocessing.cpu_count())))
    env_out = subprocess.Popen(
        [
            'bash', '-c', '. ./build/envsetup.sh;'
            'lunch ' + target + ' >/dev/null && env'
        ],
        cwd=android_base,
        stdout=subprocess.PIPE)
    env = {}
    for line in env_out.stdout:
        (key, _, value) = line.partition('=')
        value = value.strip()
        env[key] = value
    env_out.communicate()

    if redirect_stderr:
        redirect_key = compiler_wrapper.STDERR_REDIRECT_KEY
        if 'DIST_DIR' in env:
            redirect_path = os.path.join(env['DIST_DIR'], 'logs',
                                         'clang-error.log')
        else:
            redirect_path = os.path.abspath(
                os.path.join(android_base, 'out', 'clang-error.log'))
            utils.remove(redirect_path)
        env[redirect_key] = redirect_path
        fallback_path = build.clang_prebuilt_bin_dir()
        env[compiler_wrapper.PREBUILT_COMPILER_PATH_KEY] = fallback_path
        env[compiler_wrapper.DISABLED_WARNINGS_KEY] = ' '.join(
            DISABLED_WARNINGS)

    env['LLVM_PREBUILTS_VERSION'] = 'clang-dev'
    env['LLVM_RELEASE_VERSION'] = clang_version.long_version()

    if with_tidy:
        env['WITH_TIDY'] = '1'
        if 'DEFAULT_GLOBAL_TIDY_CHECKS' not in env:
            env['DEFAULT_GLOBAL_TIDY_CHECKS'] = ','.join(DEFAULT_TIDY_CHECKS)

    modules = ['dist']
    if profiler is not None:
        # Build only a subset of targets and collect profiles
        modules = ['libc', 'libLLVM_android-host64']

        key, val = profiler.getProfileFileEnvVar()
        env[key] = val

    modulesList = ' '.join(modules)
    print 'Start building target %s and modules %s.' % (target, modulesList)
    subprocess.check_call(
        ['/bin/bash', '-c', 'make ' + jobs + ' ' + modulesList],
        cwd=android_base,
        env=env)


def test_device(android_base, clang_version, device, max_jobs, clean_output,
                flashall_path, redirect_stderr, with_tidy):
    [label, target] = device[-1].split(':')
    # If current device is not connected correctly we will just skip it.
    if label != 'device':
        print 'Device %s is not connecting correctly.' % device[0]
        return True
    else:
        target = 'aosp_' + target + '-eng'
    try:
        build_target(android_base, clang_version, target, max_jobs,
                     redirect_stderr, with_tidy)
        if flashall_path is None:
            bin_path = os.path.join(android_base, 'out', 'host',
                                    utils.build_os_type(), 'bin')
            subprocess.check_call(
                ['./adb', '-s', device[0], 'reboot', 'bootloader'],
                cwd=bin_path)
            subprocess.check_call(
                ['./fastboot', '-s', device[0], 'flashall'], cwd=bin_path)
        else:
            os.environ['ANDROID_SERIAL'] = device[0]
            subprocess.check_call(['./flashall'], cwd=flashall_path)
        result = True
    except subprocess.CalledProcessError:
        print 'Flashing/testing android for target %s failed!' % target
        result = False
    if clean_output:
        rm_current_product_out()
    return result


def build_clang(instrumented=False, pgo=True):
    stage1_install = utils.out_path('stage1-install')
    stage2_install = utils.out_path('stage2-install')

    # LLVM tool llvm-profdata from stage1 is needed to merge the collected
    # profiles.  Build all LLVM tools if building instrumented stage2
    build.build_stage1(stage1_install, build_name='dev',
                       build_llvm_tools=instrumented)

    profdata = None
    if pgo:
        long_version = build.extract_clang_long_version(stage1_install)
        profdata = build.pgo_profdata_file(long_version)

    build.build_stage2(
        stage1_install,
        stage2_install,
        build.STAGE2_TARGETS,
        build_name='dev',
        build_instrumented=instrumented,
        profdata_file=profdata)
    build.build_runtimes(stage2_install)
    version = build.extract_clang_version(stage2_install)
    return stage2_install, version


def extract_packaged_clang(package_path):
    # Find package to extract
    tarballs = [f for f in os.listdir(package_path) if \
                    f.endswith('.tar.bz2') and 'linux' in f]
    if len(tarballs) != 1:
        raise RuntimeError(
            'No clang packages (.tar.bz2) found in ' + package_path)

    tarball = os.path.join(package_path, tarballs[0])

    # Extract package to $OUT_DIR/extracted
    extract_dir = utils.out_path('extracted')
    if os.path.exists(extract_dir):
        utils.rm_tree(extract_dir)
    build.check_create_path(extract_dir)

    args = ['tar', '-xjC', extract_dir, '-f', tarball]
    subprocess.check_call(args)

    # Find and return a singleton subdir
    extracted = os.listdir(extract_dir)
    if len(extracted) != 1:
        raise RuntimeError(
            'Expected one file from package.  Found: ' + ' '.join(extracted))

    clang_path = os.path.join(extract_dir, extracted[0])
    if not os.path.isdir(clang_path):
        raise RuntimeError('Extracted path is not a dir: ' + clang_path)

    return clang_path


def main():
    args = parse_args()
    if args.clang_path is not None:
        clang_path = args.clang_path
        clang_version = build.extract_clang_version(clang_path)
    elif args.clang_package_path is not None:
        clang_path = extract_packaged_clang(args.clang_package_path)
        clang_version = build.extract_clang_version(clang_path)
    else:
        clang_path, clang_version = build_clang(
            instrumented=args.profile, pgo=(not args.no_pgo))
    build.install_wrappers(clang_path)
    link_clang(args.android_path, clang_path)

    if args.build_only:
        profiler = ClangProfileHandler() if args.profile else None

        targets = [args.target] if args.target else TARGETS
        for target in targets:
            build_target(args.android_path, clang_version, target, args.jobs,
                         args.redirect_stderr, args.with_tidy, profiler)

        if profiler is not None:
            profiler.mergeProfiles()

    else:
        devices = get_connected_device_list()
        if len(devices) == 0:
            print "You don't have any devices connected."
        for device in devices:
            result = test_device(args.android_path, clang_version, device,
                                 args.jobs, args.clean_built_target,
                                 args.flashall_path, args.redirect_stderr,
                                 args.with_tidy)
            if not result and not args.keep_going:
                break


if __name__ == '__main__':
    main()
