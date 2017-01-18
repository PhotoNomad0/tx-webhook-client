from setuptools import setup, find_packages

setup(
    name="tx-webhook-client",
    version="1.0.0",
    description="tX webhook client",
    long_description="tX webhook client",
    url="https://github.com/unfoldingWord-dev/tx-webhook-client",
    author="unfoldingWord",
    author_email="info@door43.org",
    license="MIT",
    classifiers=[],
    keywords=["tx", "client"],
    packages=find_packages(),
    install_requires=["future", "requests"],
    test_suite="tests"
)
