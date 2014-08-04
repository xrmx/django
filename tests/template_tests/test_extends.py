from django.conf import settings
from django.template import Context
from django.template.loader import get_template
from django.test import TestCase, override_settings

@override_settings(ROOT_URLCONF="admin_views.urls")
class Tests(TestCase):
    """
    Test that ``extends`` triggers across multiple applications templates
    with the same relative path.
    """
    def setUp(self):
        settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ['django.contrib.admin']

    def test_extends(self):
        """
        Ensure the test string appear in the rendered template.
        """
        html = get_template("admin/base.html").render(Context())
        self.assertTrue("Hello from a index template that extends one with the same name" in html)
