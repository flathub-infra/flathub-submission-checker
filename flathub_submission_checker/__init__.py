from importlib import metadata

__version__ = metadata.version(__package__ or "flathub_submission_checker")
del metadata
