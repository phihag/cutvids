default: test

test:
	flake8 .
	python -m unittest discover

.PHONY: default test
