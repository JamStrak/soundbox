"""Cooperative Uvicorn runner used by the ordinary hidden launcher."""
import argparse
import uvicorn
from .app import app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=23176)
    args = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port))
    app.state.hub_lifecycle.shutdown = lambda: setattr(server, 'should_exit', True)
    server.run()


if __name__ == '__main__':
    main()
