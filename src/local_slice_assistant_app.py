"""PyInstaller 的绝对导入入口，避免把包内 __main__.py 当成孤立脚本。"""

from pathlib import Path
import sys


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--portable-export-check':
        from local_slice_assistant.portable_smoke import main as export_check
        return export_check(sys.argv[2])
    # Both release executables share the same bundled modules and media tools.
    # Only the console executable owns stdin/stdout for automation.
    if Path(sys.executable).stem.casefold() == 'localsliceassistantcli' or '--cli' in sys.argv[1:]:
        from local_slice_assistant.cli import main as cli_main
        arguments = sys.argv[1:]
        if arguments and arguments[0] == '--cli':
            arguments = arguments[1:]
        return cli_main(arguments)
    from local_slice_assistant.gui import main as gui_main
    return gui_main()


if __name__ == '__main__':
    raise SystemExit(main())
