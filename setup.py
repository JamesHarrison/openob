#!/usr/bin/env python

from distutils.core import setup

setup(name='OpenOB',
      version='2.3',
      description='Broadcast audio over IP codec built with PyGST',
      author='James Harrison',
      author_email='james@talkunafraid.co.uk',
      url='http://jamesharrison.github.com/openob',
      scripts=['bin/openob'],
      packages=['openob','openob.rtp'],
      requires=['pygst','colorama', 'redis'],
      classifiers=["Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 2",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Natural Language :: English",
        "Operating System :: POSIX :: Linux",
        "Topic :: Communications",
        "Topic :: Internet",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Environment :: No Input/Output (Daemon)",
        "Intended Audience :: Telecommunications Industry",
        "Intended Audience :: System Administrators",
        "Intended Audience :: Developers"
      ]


     )
