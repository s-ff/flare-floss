# Copyright (C) 2023 Mandiant, Inc. All Rights Reserved.
import sys
import hashlib
import logging
import pathlib
import argparse
import textwrap
from typing import List

import pefile
import tabulate
from floss.utils import get_static_strings
from floss.results import StaticString, StringEncoding
from floss.render.sanitize import sanitize
from floss.language.go.extract import extract_go_strings

logger = logging.getLogger(__name__)

MIN_STR_LEN = 4


def main():
    parser = argparse.ArgumentParser(description="Get Go strings")
    parser.add_argument("path", help="file or path to analyze")
    parser.add_argument(
        "-n",
        "--minimum-length",
        dest="min_length",
        type=int,
        default=MIN_STR_LEN,
        help="minimum string length",
    )
    logging_group = parser.add_argument_group("logging arguments")
    logging_group.add_argument("-d", "--debug", action="store_true", help="enable debugging output on STDERR")
    logging_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="disable all status output except fatal errors",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger().setLevel(logging.INFO)

    try:
        pe = pefile.PE(args.path)
    except pefile.PEFormatError as err:
        logger.debug(f"NOT a valid PE file: {err}")
        return 1

    path = pathlib.Path(args.path)

    static_strings: List[StaticString] = get_static_strings(path, args.min_length)

    go_strings = extract_go_strings(path, args.min_length)

    get_extract_stats(pe, static_strings, go_strings, args.min_length)
    get_extract_stats_old(pe, static_strings, go_strings, "arker")

    with open("extracted-arker.txt", "w", encoding="utf-8") as f:
        for s in go_strings:
            sec = pe.get_section_by_rva(s.offset)
            if sec:
                secname = sec.Name.decode("utf-8").split("\x00")[0]
            else:
                secname = "N/A"
            f.write(f"0x{s.offset:08x} {secname:8s} {s.string}\n")


def get_extract_stats_old(pe, all_ss_strings, go_strings, suffix):
    target_strings = ""
    for ss in all_ss_strings:
        sec = pe.get_section_by_rva(ss.offset)
        if sec:
            secname = sec.Name.decode("utf-8").split("\x00")[0]
            if secname in (".text", ".data", ".rdata"):
                target_strings += ss.string

    gs_len = 0
    ts_len = len(target_strings)
    target_strings_original = target_strings
    target_strings_replaced = target_strings

    for gs in go_strings:
        gs = gs.string
        if gs and gs in target_strings:
            gs_len += len(gs)
            target_strings = target_strings.replace(gs, "", 1)
            target_strings_replaced = target_strings_replaced.replace(gs, "=" * len(gs), 1)

    with open(f"found-{suffix}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(textwrap.wrap(target_strings_replaced, width=160)))
    with open(f"origi-{suffix}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(textwrap.wrap(target_strings_original, width=160)))
    print("len targets  :", ts_len)
    print("len gostrings:", gs_len)
    print("len removed  :", len(target_strings))
    print(f"Percentage of strings extracted: {round(100 * (gs_len / ts_len))}%")
    print(f"Percentage of missed strings   : {round(100 * (len(target_strings) / ts_len))}%")


def get_extract_stats(pe, all_ss_strings: List[StaticString], go_strings, min_len):
    all_strings = list()
    # these are ascii, extract these utf-8 to get fewer chunks (ascii may split on two-byte characters, for example)
    for ss in all_ss_strings:
        sec = pe.get_section_by_rva(ss.offset)
        secname = sec.Name.decode("utf-8").split("\x00")[0] if sec else ""
        all_strings.append((secname, ss))

    len_all_ss = 0
    len_gostr = 0

    gs_found = list()
    results = list()
    for secname, s in all_strings:
        if secname != ".rdata":
            continue

        if len(s.string) <= 30:
            # guessed value right now
            continue

        len_all_ss += len(s.string)

        orig_len = len(s.string)
        sha256 = hashlib.sha256()
        sha256.update(s.string.encode("utf-8"))
        s_id = sha256.hexdigest()[:3].upper()
        s_range = (s.offset, s.offset + len(s.string))

        found = False
        for gs in go_strings:
            sec = pe.get_section_by_rva(gs.offset)
            gs_sec = sec.Name.decode("utf-8").split("\x00")[0] if sec else ""

            if gs_sec != ".rdata":
                continue

            if (
                gs.string
                and gs.string in s.string
                and gs_sec == secname
                and s.offset <= gs.offset <= s.offset + orig_len
            ):
                found = True
                len_gostr += len(gs.string)

                # remove found string data
                idx = s.string.find(gs.string)
                assert idx != -1
                if idx == 0:
                    new_offset = s.offset + idx + len(gs.string)
                else:
                    new_offset = s.offset

                replaced_s = s.string.replace(gs.string, "", 1)
                replaced_len = len(replaced_s)
                s_trimmed = StaticString(
                    string=replaced_s,
                    offset=new_offset,
                    encoding=s.encoding,
                )

                type_ = "substring"
                if s.string[: len(gs.string)] == gs.string and s.offset == gs.offset:
                    type_ = "exactsubstr"

                results.append((secname, s_id, s_range, True, type_, s, replaced_len, gs))

                s = s_trimmed

                gs_found.append(gs)

                if replaced_len < min_len:
                    results.append((secname, s_id, s_range, "", "missing", s, orig_len - replaced_len, gs))
                    break

        if not found:
            null = StaticString(string="", offset=0, encoding=StringEncoding.UTF8)
            results.append((secname, s_id, s_range, False, "", s, 0, null))

    rows = list()
    for gs in go_strings:
        sec = pe.get_section_by_rva(gs.offset)
        gs_sec = sec.Name.decode("utf-8").split("\x00")[0] if sec else ""
        if gs_sec != ".rdata":
            continue

        if gs in gs_found:
            continue

        gsdata = gs.string  # if gs.string else ''
        if len(gs.string) >= 50:
            gsdata = gs.string[:36] + "...." + gs.string[-10:]
        gsdata = sanitize(gsdata)

        rows.append(
            (
                f"{gs_sec}",
                f"",
                f"",
                f"{gs.offset:8x}",
                f"",
                f"unmatched go string",
                f"",
                f"",
                f"{len(gs.string) if gs.string else ''}",
                f"{gsdata}",
                f"{hex(gs.offset) if gs.offset else ''}",
            )
        )

    for r in results:
        secname, s_id, s_range, found, msg, s, len_after, gs = r

        sdata = s.string
        if len(s.string) >= 50:
            sdata = s.string[:36] + "...." + s.string[-10:]
        sdata = sanitize(sdata)

        gsdata = gs.string  # if gs.string else ''
        if len(gs.string) >= 50:
            gsdata = gs.string[:36] + "...." + gs.string[-10:]
        gsdata = sanitize(gsdata)

        len_info = f"{len(s.string):3d}"
        if found:
            len_info = f"{len(s.string):3d} > {len_after:3d} ({(len(s.string) - len_after) * -1:2d})"

        rows.append(
            (
                f"{secname}",
                f"<{s_id}>",
                f"{s_range[0]:x} - {s_range[1]:x}",
                f"{s.offset:8x}",
                f"{found}",
                f"{msg}",
                len_info,
                f"{sdata}",
                f"{len(gs.string) if gs.string else ''}",
                f"{gsdata}",
                f"{hex(gs.offset) if gs.offset else ''}",
            )
        )

    print(
        tabulate.tabulate(
            sorted(rows, key=lambda t: t[3]),
            headers=[
                "section",
                "id",
                "range",
                "offset",
                "found",
                "msg",
                "slen",
                "string",
                "gslen",
                "gostring",
                "gsoff",
            ],
            tablefmt="psql",
        )
    )

    print(".rdata only")
    print("len all string chars:", len_all_ss)
    print("len gostring chars  :", len_gostr)
    print(f"Percentage of string chars extracted: {round(100 * (len_gostr / len_all_ss))}%")
    print()


def get_missed_strings(all_ss_strings: List[StaticString], go_strings, min_len):
    # TODO unused, but use?
    len_all_ss = 0
    len_gostr = 0

    for s in all_ss_strings:
        len_all_ss += len(s.string)

        orig_len = len(s.string)

        found = False
        for gs in go_strings:
            if (
                gs.string
                and gs.string in s.string
                and s.offset <= gs.offset <= s.offset + orig_len
            ):
                found = True
                len_gostr += len(gs.string)

                # remove found string data
                idx = s.string.find(gs.string)
                assert idx != -1
                if idx == 0:
                    new_offset = s.offset + idx + len(gs.string)
                else:
                    new_offset = s.offset

                replaced_s = s.string.replace(gs.string, "", 1)
                replaced_len = len(replaced_s)
                s_trimmed = StaticString(
                    string=replaced_s,
                    offset=new_offset,
                    encoding=s.encoding,
                )
                s = s_trimmed

                if replaced_len < min_len:
                    break

        if not found:
            yield s


if __name__ == "__main__":
    sys.exit(main())
