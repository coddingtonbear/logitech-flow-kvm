import argparse
import sys


def main(args=sys.argv):
    parser = argparse.ArgumentParser(description='Command description.')
    args = parser.parse_args(args=args[1:])
