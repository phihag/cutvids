default: test

test:
	flake8 .
	python -m unittest

.PHONY: default test
