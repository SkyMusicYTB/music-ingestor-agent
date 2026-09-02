"""Persistent media worker package.

Worker components are intentionally imported from their concrete modules. Keeping
this package initializer side-effect free prevents client helpers from loading the
entire worker graph merely to use the shared bounded subprocess runner.
"""
