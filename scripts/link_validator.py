#!/usr/bin/env python3
"""Validate [[wiki-links]] across all vault Markdown files.

Scans all .md files, extracts [[wiki-links]], and verifies targets exist.
Supports Obsidian's bare-filename resolution ([[my-note]] resolves to
any file named my-note.md regardless of directory).

Usage:
  python link_validator.py <vault_path>
"""

import os
import re
import sys

from safety import (
    VAULT_INTERNAL_DIR_NAMES,
    split_frontmatter_text,
    strip_markdown_code_blocks,
)


WIKI_LINK_PATTERN = re.compile(r'\[\[([^\]\r\n]+)\]\]')
DEFAULT_EXCLUDED_SOURCE_DIRS = frozenset({"02-Templates"})
DEFAULT_EXCLUDED_SOURCE_FILENAMES = frozenset({"_TEMPLATE.md"})


def extract_wikilink_targets(content):
    """Return link targets while ignoring code examples and Markdown aliases."""
    content = strip_markdown_code_blocks(content)
    targets = []
    for raw_link in WIKI_LINK_PATTERN.findall(content):
        target = re.split(r"\\?\|", raw_link, maxsplit=1)[0].strip()
        if target:
            targets.append(target)
    return targets


def run(vault_path, excluded_dir_names=(), additional_markdown_paths=()):
    """Scan all .md files, extract [[links]], verify targets exist.

    Reusable Vault templates remain in the target index, but their placeholder
    links are not validated unless the file is passed explicitly.
    Explicit additional Markdown files are validated against the Vault index
    even when they live in an excluded directory or outside the Vault.

    Returns list of broken links with {source, target, reason} dicts.
    """
    if isinstance(excluded_dir_names, str):
        excluded_dir_names = {excluded_dir_names}
    else:
        try:
            excluded_dir_names = set(excluded_dir_names or ())
        except TypeError as exc:
            raise TypeError(
                "excluded_dir_names must be a string or iterable of strings"
            ) from exc
    if not all(isinstance(name, str) for name in excluded_dir_names):
        raise TypeError("excluded_dir_names entries must be strings")
    ignored_dirs = VAULT_INTERNAL_DIR_NAMES | excluded_dir_names
    vault_path = os.path.abspath(os.fspath(vault_path))
    additional_markdown_paths = tuple(
        dict.fromkeys(
            os.path.abspath(os.fspath(path))
            for path in additional_markdown_paths or ()
        )
    )

    def source_name(path):
        try:
            inside_vault = os.path.commonpath([vault_path, path]) == vault_path
        except ValueError:
            inside_vault = False
        if inside_vault:
            return os.path.relpath(path, vault_path).replace('\\', '/')
        return path

    def raise_walk_error(error):
        raise error

    # Build file index
    file_index = {}
    anchor_index = {}

    for root, dirs, files in os.walk(vault_path, onerror=raise_walk_error):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for f in files:
            if not f.endswith('.md'):
                continue
            fp = os.path.join(root, f)
            rel = source_name(fp)
            file_index[rel.lower()] = rel

            # Extract anchors from this file
            with open(fp, 'r', encoding='utf-8') as fh:
                content = fh.read()
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is not None:
                import yaml
                try:
                    fm = yaml.safe_load(frontmatter_text)
                    if fm and 'anchor' in fm:
                        anchor_index[
                            f"{rel}#{fm['anchor']}".lower()
                        ] = rel
                except yaml.YAMLError:
                    pass

    for fp in additional_markdown_paths:
        if not fp.endswith('.md'):
            raise ValueError(f"additional Markdown path must end in .md: {fp}")
        rel = source_name(fp)
        file_index[rel.lower()] = rel

    # Build filename-only index for Obsidian bare-filename resolution
    # e.g., [[my-memory]] resolves to any file named my-memory.md
    filename_index = {}
    for rel in file_index.values():
        basename = os.path.basename(rel).lower()
        name_no_ext = basename.replace('.md', '')
        if name_no_ext not in filename_index:
            filename_index[name_no_ext] = []
        filename_index[name_no_ext].append(rel)

    # Validate all links
    broken = []
    validated_paths = set()

    def validate_path(fp):
        fp = os.path.abspath(fp)
        if fp in validated_paths:
            return
        validated_paths.add(fp)
        with open(fp, 'r', encoding='utf-8') as fh:
            content = fh.read()

        links = extract_wikilink_targets(content)
        for link in links:
            target = link.strip().replace('\\', '/').lower()
            if target.startswith('#'):
                continue
            base = target.split('#')[0]
            if not base.endswith('.md'):
                base += '.md'

            if base in file_index or target in anchor_index:
                continue

            # Fallback: Obsidian filename-only resolution
            search_name = os.path.splitext(os.path.basename(base))[0]
            if search_name in filename_index:
                continue

            broken.append({
                'source': source_name(fp),
                'target': link.strip(),
                'reason': 'file not found'
            })

    for root, dirs, files in os.walk(vault_path, onerror=raise_walk_error):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        if os.path.abspath(root) == vault_path:
            dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDED_SOURCE_DIRS]
        for f in files:
            if not f.endswith('.md') or f in DEFAULT_EXCLUDED_SOURCE_FILENAMES:
                continue
            validate_path(os.path.join(root, f))

    for fp in additional_markdown_paths:
        validate_path(fp)

    return broken


def main():
    if len(sys.argv) < 2:
        print("Usage: python link_validator.py <vault_path>")
        sys.exit(1)

    vault = sys.argv[1]
    if not os.path.exists(vault):
        print(f"ERROR: Vault path not found: {vault}")
        sys.exit(1)

    broken = run(vault)
    if broken:
        print(f"Broken links: {len(broken)}")
        for b in broken:
            print(f"  {b['source']} -> [[{b['target']}]] ({b['reason']})")
        sys.exit(1)
    else:
        print("All wiki-links valid")


if __name__ == '__main__':
    main()
