from setuptools import setup

setup(
    name="tx-webhook-client",
    package_dir={'webhook': 'functions/webhook',
                 'callback': 'functions/callback'},
    packages=['webhook', 'callback'],
    version="0.0.1",
    description="tX webhook client",
    long_description="tX webhook client",
    url="https://github.com/unfoldingWord-dev/tx-webhook-client",
    author="unfoldingWord",
    author_email="info@door43.org",
    license="MIT",
    classifiers=[],
    keywords=["tx", "client"],
    install_requires=["future", "requests"]
)
