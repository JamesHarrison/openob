#!/usr/bin/env python

from setuptools import setup
setup(name='OpenOB',
      version='4.0.1',
      description='Broadcast audio over IP codec built with PyGST',
      author='James Harrison',
      author_email='james@talkunafraid.co.uk',
      url='https://github.com/JamesHarrison/openob',
      scripts=['bin/openob'],
      install_requires=['redis'],
      packages=['openob', 'openob.rtp'],
      classifiers=["Programming Language :: Python",
                   "Programming Language :: Python :: 3",
                   "Programming Language :: Python :: 2",
                   "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
                   "Natural Language :: English",
                   "Operating System :: POSIX :: Linux",
                   "Topic :: Communications",
                   "Topic :: Internet",
                   "Topic :: Software Development :: Libraries :: Python Modules",
                   "Development Status :: 5 - Production/Stable",
                   "Environment :: Console",
                   "Environment :: No Input/Output (Daemon)",
                   "Intended Audience :: Telecommunications Industry",
                   "Intended Audience :: System Administrators",
                   "Intended Audience :: Developers"
                   ]


      )
