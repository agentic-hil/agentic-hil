#!/bin/sh
set -eu

archive=${1:?archive path required}
auto_install=${2:?auto-install XML path required}
expected_sha256=6a9e60a5a048c45eb3241f9bb66bdc2e6cbd0119fb2e42568dc059fc6167442a

printf '%s  %s\n' "$expected_sha256" "$archive" | sha256sum --check --status || {
    echo "STM32CubeProgrammer installer archive SHA-256 mismatch" >&2
    exit 1
}

temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
unzip -q "$archive" -d "$temporary_directory"
installer_jar=$(find "$temporary_directory" -type f -name SetupSTM32CubeProgrammer-2.23.0.exe -print -quit)
if [ -z "$installer_jar" ]; then
    echo "STM32CubeProgrammer installer JAR was not found in the archive" >&2
    exit 1
fi
installer_directory=${installer_jar%/*}
cp "$auto_install" "$installer_directory/auto-check.xml"
chmod +x "$installer_directory/jre/bin/java"

# The installer resolves its bundled JRE and payload relative to its current
# directory; running the JAR from elsewhere makes its internal `cp jre` fail.
cd "$installer_directory"
./jre/bin/java -Djava.awt.headless=true -jar SetupSTM32CubeProgrammer-2.23.0.exe auto-check.xml

cli=/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI
test -x "$cli"
"$cli" -q --version | grep -F "STM32CubeProgrammer version: 2.23.0" >/dev/null
