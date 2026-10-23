import sys

from .kafka_io import main

sys.exit(main(sys.argv[1:]))
