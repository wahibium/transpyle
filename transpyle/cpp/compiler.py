""""Compiling of C++."""

from distutils.sysconfig import get_python_inc, get_config_vars
import logging
import os
import pathlib
import platform
import shutil
import subprocess
import tempfile
import typing as t

import argunparse
# from static_typing.ast_manipulation import RecursiveAstVisitor
# import typed_ast.ast3 as typed_ast3

from ..general import Language, CodeReader, Parser, AstGeneralizer, Unparser, Compiler
from ..general.tools import run_tool

PYTHON_LIB_PATH = pathlib.Path(get_python_inc(plat_specific=1))

SWIG_INTERFACE_TEMPLATE = '''/* File: {module_name}.i */
/* Generated by transpyle. */
%module {module_name}

%{{
#define SWIG_FILE_WITH_INIT
{include_directives}
%}}

{function_signatures}
'''

SWIG_INTERFACE_TEMPLATE_HPP = '''/* File: {module_name}.i */
/* Generated by transpyle. */
%module {module_name}

%{{
#define SWIG_FILE_WITH_INIT
#include "{include_path}"
%}}

%include "{include_path}"

// below is Python 3 support, however,
// adding it will generate wrong .so file
// for Fedora 25 on ARMv7. So be sure to
// comment them when you compile for
// Fedora 25 on ARMv7.
%begin %{{
#define SWIG_PYTHON_STRICT_BYTE_CHAR
%}}
'''

_LOG = logging.getLogger(__name__)


class SwigCompiler(Compiler):

    """SWIG-based compiler."""

    def __init__(self, language: Language):
        super().__init__()
        self.language = language
        self.argunparser = argunparse.ArgumentUnparser()

    def create_header_file(self, path: pathlib.Path) -> str:
        """Create a header for a given C/C++ source code file."""
        code_reader = CodeReader()
        parser = Parser.find(self.language)()
        ast_generalizer = AstGeneralizer.find(self.language)({'path': path})
        unparser = Unparser.find(self.language)(headers=True)
        code = code_reader.read_file(path)
        cpp_tree = parser.parse(code, path)
        tree = ast_generalizer.generalize(cpp_tree)
        header_code = unparser.unparse(tree)
        _LOG.debug('unparsed raw header file: """%s"""', header_code)
        return header_code

    def _create_swig_interface(self, path: pathlib.Path) -> str:
        """Create a SWIG interface for a given C/C++ source code file."""
        module_name = path.with_suffix('').name
        header_code = self.create_header_file(path)
        include_directives = []
        function_signatures = []
        for line in header_code.splitlines():
            if line.startswith('#include'):
                collection = include_directives
            else:
                collection = function_signatures
            collection.append(line)
        swig_interface = SWIG_INTERFACE_TEMPLATE.format(
            module_name=module_name, include_directives='\n'.join(include_directives),
            function_signatures='\n'.join(function_signatures))
        _LOG.debug('SWIG interface: """%s"""', swig_interface)
        return swig_interface

    def create_swig_interface(self, path: pathlib.Path) -> str:
        """Create a SWIG interface for a given C/C++ header file."""
        module_name = path.with_suffix('').name
        swig_interface = SWIG_INTERFACE_TEMPLATE_HPP.format(
            module_name=module_name, include_path=path)
        _LOG.debug('SWIG interface: """%s"""', swig_interface)
        return swig_interface

    def run_swig(self, interface_path: pathlib.Path, *args) -> subprocess.CompletedProcess:
        """Run SWIG.

        For C extensions:
        swig -python example.i

        If building a C++ extension, add the -c++ option:
        swig -c++ -python example.i
        """
        swig_cmd = ['swig', '-python', *args, str(interface_path)]
        _LOG.info('running SWIG via %s', swig_cmd)
        return run_tool(pathlib.Path(swig_cmd[0]), swig_cmd[1:])


class CppSwigCompiler(SwigCompiler):

    """SWIG-based compiler for C++."""

    py_config = get_config_vars()
    cpp_flags = ('-O3', '-fPIC', '-fopenmp')

    def __init__(self):
        super().__init__(Language.find('C++'))

    def run_gpp(self, *args) -> subprocess.CompletedProcess:
        compiler = {'Linux': 'g++', 'Darwin': 'clang++'}[platform.system()]
        gcc_cmd = [compiler, *args]
        _LOG.warning('running C++ compiler: %s', gcc_cmd)
        return run_tool(pathlib.Path(compiler), args)

    def run_cpp_compiler(self, path: pathlib.Path,
                         wrapper_path: pathlib.Path = None) -> subprocess.CompletedProcess:
        # gcc -c example.c example_wrap.c -I/usr/local/include/python2.1
        flags = '-I{} {} {}'.format(
            self.py_config['INCLUDEPY'],
            self.py_config['BASECFLAGS'], self.py_config['BASECPPFLAGS']).split()
        flags = [_.strip() for _ in flags if _.strip()]
        gcc_args = [*self.cpp_flags, *flags,
                    '-c', str(path), str(wrapper_path)]
        return self.run_gpp(*gcc_args)

    def run_cpp_linker(self, path: pathlib.Path,
                       wrapper_path: pathlib.Path = None) -> subprocess.CompletedProcess:
        # ld -shared example.o example_wrap.o -o _example.so
        ldlibrary = pathlib.Path(self.py_config['LDLIBRARY'].lstrip('lib')).with_suffix('')
        flags = '-L{} -l{} {} {} {}'.format(
            self.py_config['LIBDIR'], ldlibrary, self.py_config['LIBS'],
            self.py_config['SYSLIBS'], self.py_config['LINKFORSHARED']).split()
        flags = [_.strip() for _ in flags if _.strip()]
        linker_args = [*self.cpp_flags, *flags,
                       '-shared', str(path.with_suffix('.o')), str(wrapper_path.with_suffix('.o')),
                       '-o', '{}'.format(path.with_name('_' + path.name).with_suffix('.so'))]
        return self.run_gpp(*linker_args)

    def compile(self, code: str, path: t.Optional[pathlib.Path] = None,
                output_folder: t.Optional[pathlib.Path] = None, **kwargs) -> pathlib.Path:
        if output_folder is None:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_folder = pathlib.Path(tmpdir)
            output_folder.mkdir()
        header_code = self.create_header_file(path)
        hpp_path = output_folder.joinpath(path.name).with_suffix('.hpp')
        with hpp_path.open('w') as header_file:
            header_file.write(header_code)
        swig_interface = self.create_swig_interface(hpp_path.relative_to(output_folder))
        cpp_path = output_folder.joinpath(path.name)
        shutil.copy2(str(path), str(cpp_path))
        swig_interface_path = output_folder.joinpath(path.with_suffix('.i').name)
        with swig_interface_path.open('w') as swig_interface_file:
            swig_interface_file.write(swig_interface)
        wrapper_path = output_folder.joinpath(path.with_suffix('').name + '_wrap.cxx')

        cwd = os.getcwd()
        os.chdir(str(output_folder))
        result = self.run_swig(swig_interface_path, '-c++')
        if result.returncode != 0:
            raise RuntimeError('{} -- Failed to create SWIG interface for "{}":\n"""\n{}"""\n'
                               'The header "{}" is:\n"""{}"""\nExamine folder "{}" for details'
                               .format(result.args, path, result.stderr.decode(), hpp_path,
                                       header_code, output_folder))
        result = self.run_cpp_compiler(cpp_path, wrapper_path)
        assert result.returncode == 0
        result = self.run_cpp_linker(cpp_path, wrapper_path)
        assert result.returncode == 0
        os.chdir(cwd)

        return cpp_path.with_suffix('.py')
