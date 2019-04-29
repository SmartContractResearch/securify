"""
Author: Tobias Kaiser

Copyright 2018 ChainSecurity AG

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import os
import re
import operator
from collections import namedtuple
from distutils.version import StrictVersion
import sys
import json
import requests
import requests.exceptions
import subprocess
import logging

from solcx.main import _parse_compiler_output
from solcx.wrapper import solc_wrapper
from solcx.exceptions import SolcError
from solcx import get_installed_solc_versions
from solcx import get_solc_folder
from solcx import install_solc

from .utils import find_node_modules_dir

MINIMAL_SOLC_VERSION = "0.4.11"

class NoSolidityProject(BaseException):
    def __init__(self, dirpath):
        self.dir = dirpath


class CompilerVersionNotSupported(BaseException):
    pass


class SolidityCompilationException(SolcError):
    def __init__(self, solc_exception, files):
        super().__init__(
            solc_exception.command,
            solc_exception.return_code,
            solc_exception.stdin_data,
            solc_exception.stderr_data,
            solc_exception.stdout_data,
            solc_exception.message
        )
        self.files = files


class OffsetException(Exception):
    pass

RELEASES_LIST = "https://api.github.com/repos/ethereum/solidity/releases"

OUTPUT_VALUES = ('abi',
                 'ast',
                 'bin-runtime',
                 'srcmap-runtime')

class SolidityVersion(StrictVersion):
    """Class to define a solidity version
    inherits comparators from StrictVersion and adds one for the case '^x.y.z'
    """
    def __xor__(self, other):
        return self.version[0] == other.version[0] and \
               self.version[1:] >= other.version[1:]

    def __str__(self):
        s = super(SolidityVersion, self).__str__()
        # Fix for "0.5"
        if len(s) <= 3:
            s += ".0"
        return s

SOLC_VERSIONS = []

OperatorVersionTuple = namedtuple('OperatorVersionTuple', ['op', 'v'])

# grouping matches into operator and version part
# e.g for >=0.4.24 it would group >= and 0.4.24
comp_version_rex = re.compile(r'(?P<operator>(<|>|>=|<=|\^)?)'
                              r'(?P<version>\d+\.\d+\.\d+)')

ops = {
    '>': operator.gt,
    '<': operator.lt,
    '': operator.eq,
    '>=': operator.ge,
    '<=': operator.le,
    '^': operator.xor
}

def _get_binary(solc_version):
    """Returns the binary for some version of solc.
    """
    binary = os.path.join(get_solc_folder(), f'solc-v{solc_version}')
    if not os.path.exists(binary):
        raise AssertionError(f'solc binary not found for version: {solc_version}')
    return binary

def set_supported_solc_versions():
    global SOLC_VERSIONS
    # If online, try to fetch the available versions
    try:
        releases = requests.get(RELEASES_LIST).json()
    except requests.exceptions.RequestException:
        # If offline, work with installed versions
        logging.info('Fetching the latest compiler releases failed, relying on installed versions.')
        versions = [SolidityVersion(v[1:])
                    for v in get_installed_solc_versions()]
        SOLC_VERSIONS = [v for v in versions if v >= SolidityVersion(MINIMAL_SOLC_VERSION)]
        return

    versions = []
    for release in releases:
        newversion = SolidityVersion(release["tag_name"][1:])
        logging.info(f'Parsing version v{newversion}')
        # Require a minimum version 
        if newversion < SolidityVersion(MINIMAL_SOLC_VERSION):
            logging.info(f'Version v{newversion} is too small')
            continue
        # Require a precompiled binary
        for asset in release["assets"]:
            if asset["name"] == "solc-static-linux":
                break
        else:
            logging.info(f'Version v{newversion} has no solc')
            continue
        logging.info(f'Version v{newversion} added')
        versions.append(newversion)

    SOLC_VERSIONS = sorted(versions)
    logging.info(f'Available versions v{SOLC_VERSIONS}')
        

def parse_version(source):
    with open(source, encoding='utf-8') as f:
        lines = f.readlines()

    for l in lines:
        if 'pragma' in l and not 'experimental' in l:
            conditions = list(map(
                lambda v: OperatorVersionTuple(
                    ops[v[1]], SolidityVersion(v[2])),
                comp_version_rex.findall(l))
            )

            def fullfills_all_conditions(v):
                return all(map(lambda cond: cond.op(v, cond.v), conditions))
            try:
                return min(filter(fullfills_all_conditions, SOLC_VERSIONS))
            except ValueError:
                raise CompilerVersionNotSupported("Conflicting Compiler Requirements")
    else:
        return SOLC_VERSIONS[-1]


def compile_solfiles(files, proj_dir, solc_version=None, output_values=OUTPUT_VALUES, remappings=None):
    def complete_remapping(remapping):
        name, old_path = remapping.split('=')
        new_path = os.path.join(proj_dir, old_path)
        return f'{name}={new_path}'

    if remappings is None:
        remappings = []
    remappings = [complete_remapping(remapping) for remapping in remappings]
    node_modules_dir = find_node_modules_dir(proj_dir)
    if node_modules_dir is not None:
        zeppelin_path = os.path.abspath(os.path.join(node_modules_dir, 'zeppelin-solidity'))
        open_zeppelin_path = os.path.abspath(os.path.join(node_modules_dir, 'openzeppelin-solidity'))
        if os.path.isdir(zeppelin_path):
            remappings.append(f'zeppelin-solidity={zeppelin_path}')
        if os.path.isdir(open_zeppelin_path):
            remappings.append(f'openzeppelin-solidity={open_zeppelin_path}')

    if solc_version is None:
        set_supported_solc_versions()
        if len(SOLC_VERSIONS) == 0:
            raise CompilerVersionNotSupported("No compiler available. No connection to GitHub?")
        solc_version = max(map(parse_version, files))

    try:
        install_solc(f'v{solc_version}')
    except (requests.exceptions.ConnectionError, subprocess.CalledProcessError):
        raise CompilerVersionNotSupported(f'Failed to install v{solc_version} compiler.')

    binary = _get_binary(solc_version)

    combined_json = ','.join(output_values)
    compiler_kwargs = {
        'import_remappings': remappings,
        'allow_paths': proj_dir,
        'source_files': files,
        'solc_binary': binary,
        'combined_json': combined_json
    }


    try:
        stdoutdata, _, _, _ = solc_wrapper(**compiler_kwargs)
        return _parse_compiler_output(stdoutdata)
    except SolcError as e:
        raise SolidityCompilationException(e, files)


def compile_project(path, remappings=None):
    sources = get_sol_files(path)
    if not sources:
        raise NoSolidityProject(path)
    return compile_solfiles(sources, path)


def get_sol_files(project_root):
    """Returns the solidity files contained in the project.
    """
    sources = []
    test_sources = []
    for p, _, fs in os.walk(project_root):
        for f in fs:
            if f.endswith('.sol'):
                if 'node_modules' not in p and '/test/' not in p[len(str(project_root)):] and not p.endswith('/test'):
                    sources.append(os.path.join(p, f))
                else:
                    test_sources.append(os.path.join(p, f))
    if len(sources) > 0:
        return sources
    else:
        return test_sources


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('Usage: %s PROJECT OUTPUT' % sys.argv[0])

    res = compile_project(sys.argv[1])

    if sys.argv[2] == '-':
        print(res)
    else:
        with open(sys.argv[2], 'w') as fs:
            json.dump(res, fs)
