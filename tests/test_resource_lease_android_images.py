from pathlib import Path
import tempfile
import unittest

from resourcelease.backends.android_images import installed_image, list_images
from resourcelease.model import LeaseError


EXTENSION = "system-images;android-33-ext5;google_apis_playstore;arm64-v8a"
MINOR = "system-images;android-36.1;google_apis_playstore;arm64-v8a"
TV = "system-images;android-36;google-tv;arm64-v8a"


class AndroidImagesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sdk = Path(tmp.name).resolve()

    def install(self, package, api, revision, *, directory=None):
        directory = directory or self.sdk.joinpath(*package.split(";"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "system.img").write_bytes(b"installed image")
        (directory / "package.xml").write_text(
            f'<r:repository xmlns:r="urn:repository"><localPackage path="{package}">'
            f"<type-details><api-level>{api}</api-level></type-details>"
            f"<revision><major>{revision}</major></revision></localPackage></r:repository>",
        )
        return directory

    def test_literal_declared_identity_survives_extension_minor_and_tv_metadata(self):
        self.install(EXTENSION, "33x", 2)
        self.install(MINOR, "36.1", 4)
        self.install(TV, "36", 4)
        self.assertEqual({item.package: item.revision[0] for item in list_images(self.sdk)}, {
            EXTENSION: 2, MINOR: 4, TV: 4,
        })
        self.install(MINOR, "36.1", 5)
        self.assertEqual(installed_image(self.sdk, MINOR).revision, (5, 0, 0))

    def test_wrong_declaration_and_asset_replacement_are_not_interchangeable(self):
        directory = self.install(EXTENSION, "33x", 2)
        original = installed_image(self.sdk, EXTENSION)
        (directory / "system.img").write_bytes(b"replacement asset")
        self.assertNotEqual(installed_image(self.sdk, EXTENSION), original)
        self.install(TV, "36", 4, directory=directory)
        with self.assertRaisesRegex(LeaseError, "does not match"):
            installed_image(self.sdk, EXTENSION)
        self.assertEqual(list_images(self.sdk), [])

    def test_redirected_directory_is_not_an_installed_candidate(self):
        target = self.install(MINOR, "36.1", 4, directory=self.sdk / "untrusted")
        alias = self.sdk.joinpath(*MINOR.split(";"))
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(LeaseError, "noncanonical"):
            installed_image(self.sdk, MINOR)
        self.assertEqual(list_images(self.sdk), [])
