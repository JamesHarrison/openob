#!/usr/bin/env python

from distutils.core import setup

setup(name='OpenOB',
      version='2.0',
      description='Broadcast audio over IP codec built with PyGST',
      author='James Harrison',
      author_email='james@talkunafraid.co.uk',
      url='http://jamesharrison.github.com/openob',
      scripts=['openob-manager.py'],
      packages=['rtp'],
      requires=['pygst']
     )