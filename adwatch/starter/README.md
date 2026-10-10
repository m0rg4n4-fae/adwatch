# Starter set

Ad close buttons (`close_*.png`) and store badges (`stuck_*.png`) that ship with adwatch.
On first run they are copied into each user's shared pool, so a new user doesn't start
from zero. Every picture needs its `.png.json` file next to it (the screen size it was
captured on), which is how it gets rescaled for other phones.

To add the ones you've collected, run this from the project folder and commit the result:

    python adwatch.py pack

Pictures are named after their contents, so running it again never creates duplicates.
