"""Image studio: colleagues make images on this computer's signed-in Codex, through a node site."""

from .routes import ImageStudio, install_image_studio

__all__ = ["ImageStudio", "install_image_studio"]


def create(*, home, sites):
    return ImageStudio(home, sites)
