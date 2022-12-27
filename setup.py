#!/usr/bin/env python
# -*- encoding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function

import io
import re
from glob import glob
from os.path import basename
from os.path import dirname
from os.path import join
from os.path import splitext

from setuptools import find_packages
from setuptools import setup


def read(*names, **kwargs):
    with io.open(
        join(dirname(__file__), *names), encoding=kwargs.get("encoding", "utf8")
    ) as fh:
        return fh.read()


setup(
    name="logitech-flow-kvm",
    version="1.0.0",
    license="MIT",
    description="Quickly switch between paired devices when using a mouse and keyboard that supports Logitech Flow.",
    long_description_content_type="text/markdown",
    long_description="%s"
    % (
        re.compile("^.. start-badges.*^.. end-badges", re.M | re.S).sub(
            "", read("README.md")
        )
    ),
    author="Adam Coddington",
    author_email="me@adamcoddington.net",
    url="https://github.com/coddingtonbear/logitech-flow-kvm",
    packages=find_packages("src"),
    package_dir={"": "src"},
    py_modules=[splitext(basename(path))[0] for path in glob("src/*.py")],
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        # complete classifier list: http://pypi.python.org/pypi?%3Aaction=list_classifiers
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Unix",
        "Operating System :: POSIX",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        # uncomment if you test on these interpreters:
        # 'Programming Language :: Python :: Implementation :: IronPython',
        # 'Programming Language :: Python :: Implementation :: Jython',
        # 'Programming Language :: Python :: Implementation :: Stackless',
        "Topic :: Utilities",
    ],
    project_urls={
        "Documentation": "https://logitech-flow-kvm.readthedocs.io/",
        "Changelog": "https://logitech-flow-kvm.readthedocs.io/en/latest/changelog.html",
        "Issue Tracker": "https://github.com/coddingtonbear/logitech-flow-kvm/issues",
    },
    keywords=[
        # eg: 'keyword1', 'keyword2', 'keyword3',
    ],
    python_requires=">=3.6",
    install_requires=[
        "solaar>=1.1.8,<2.0",
        "hid_parser==0.0.3",
        "bitstruct>=8.15.1,<9.0",
        "safdie>=2.0.1,<3.0",
        "flask>=2.2.2,<3.0",
        "requests>=2.28,<3.0",
        "rich>=12.6.0,<13",
    ],
    extras_require={
        # eg:
        #   'rst': ['docutils>=0.11'],
        #   ':python_version=="2.6"': ['argparse'],
    },
    setup_requires=[
        "pytest-runner",
    ],
    entry_points={
        "console_scripts": [
            "logitech-flow-kvm = logitech_flow_kvm.cli:main",
        ],
        "logitech_flow_kvm.commands": [
            "list-devices = logitech_flow_kvm.commands.list_devices:ListDevices",
            "switch-to-host = logitech_flow_kvm.commands.switch_to_host:SwitchToHost",
            "watch = logitech_flow_kvm.commands.watch:Watch",
            "flow-server = logitech_flow_kvm.commands.flow_server:FlowServer",
            "flow-client = logitech_flow_kvm.commands.flow_client:FlowClient",
        ],
    },
)
