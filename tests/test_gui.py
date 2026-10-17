"""Build the desktop UI without starting networking or the event loop."""
import unittest
from unittest.mock import patch
import tkinter as tk
from tkinter import ttk
import app


class GuiTests(unittest.TestCase):
    def test_sessions_controls_are_available(self):
        tabs, buttons = [], []
        def check(root):
            root.withdraw()
            root.update_idletasks()
            def walk(widget):
                if isinstance(widget, ttk.Notebook):
                    tabs.extend(widget.tab(item, 'text') for item in widget.tabs())
                if isinstance(widget, ttk.Button): buttons.append(widget.cget('text'))
                for child in widget.winfo_children(): walk(child)
            walk(root)
            root.destroy()
        with patch.object(tk.Tk, 'mainloop', check), patch('threading.Thread.start'):
            app.gui()
        self.assertIn('Sessions', tabs)
        for label in ('Load session', 'Load & play', 'Launch Minecraft', 'Recover interrupted switch', 'Back up entire active session'):
            self.assertIn(label, buttons)
