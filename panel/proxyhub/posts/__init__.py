"""Automatic channel posts (M3.2-M3.4): content library, publisher, status edits.

* ``settings``: the owner's posting settings (key ``posting`` in ``settings``).
* ``plan``: when today's posts go out (pure).
* ``links``: the live links from every provider in proxyhub/links/registry.py, checked.
* ``content``: the content library (``content_items``).
* ``poster``: who sends a post (the bot today; the operator account is a later slice).
* ``publisher``: the publisher loop's steps (new posts, status edits of recent posts).

Rule 6 everywhere: a post is never deleted; a dead link is marked by EDITING the post.
"""
