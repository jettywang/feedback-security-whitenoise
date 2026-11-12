from __future__ import annotations

from io import BytesIO

from django.test import SimpleTestCase

from whitenoise.responders import Redirect, SlicedFile


class RedirectTests(SimpleTestCase):
    def get_location(self, redirect, environ):
        response = redirect.get_response("GET", environ)
        return dict(response.headers)["Location"]

    def test_location_is_root_relative(self):
        redirect = Redirect("/assets/docs/")
        location = self.get_location(redirect, {})
        assert location == "/assets/docs/"

    def test_location_includes_script_name(self):
        redirect = Redirect("/assets/docs/")
        location = self.get_location(redirect, {"SCRIPT_NAME": "/subdir"})
        assert location == "/subdir/assets/docs/"

    def test_root_script_name_does_not_create_scheme_relative_location(self):
        redirect = Redirect("/assets/docs/")
        location = self.get_location(redirect, {"SCRIPT_NAME": "/"})
        assert location == "/assets/docs/"
        assert not location.startswith("//")

    def test_location_quotes_non_ascii_characters(self):
        redirect = Redirect("/assets/中文目录/")
        location = self.get_location(redirect, {})
        assert location == "/assets/%E4%B8%AD%E6%96%87%E7%9B%AE%E5%BD%95/"

    def test_location_quotes_script_name(self):
        redirect = Redirect("/assets/docs/")
        # WSGI servers percent-decode SCRIPT_NAME as ISO-8859-1
        script_name = "/子目录".encode().decode("iso-8859-1")
        location = self.get_location(redirect, {"SCRIPT_NAME": script_name})
        assert location == "/%E5%AD%90%E7%9B%AE%E5%BD%95/assets/docs/"

    def test_extra_headers_are_preserved(self):
        redirect = Redirect("/assets/docs/", headers={"Cache-Control": "max-age=60"})
        response = redirect.get_response("GET", {})
        assert ("Cache-Control", "max-age=60") in response.headers


class SlicedFileTests(SimpleTestCase):
    def test_close_does_not_rerun_on_del(self):
        """
        Regression test for the subtle close() behaviour of SlicedFile that
        could lead to database connection errors.

        https://github.com/evansd/whitenoise/pull/612
        """
        file = BytesIO(b"1234567890")
        sliced_file = SlicedFile(file, 1, 2)

        # Emulate how Django patches the file object's close() method to be
        # response.close() and count the calls.
        # https://github.com/django/django/blob/345a6652e6a15febbf4f68351dcea5dd674ea324/django/core/handlers/wsgi.py#L137-L140
        calls = 0

        file_close = sliced_file.close

        def closer():
            nonlocal calls, file_close
            calls += 1
            if file_close is not None:
                file_close()
                file_close = None

        sliced_file.close = closer

        sliced_file.close()
        assert calls == 1

        # Deleting the sliced file should not call close again.
        del sliced_file
        assert calls == 1
