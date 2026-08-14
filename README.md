InkWell is a markup editor that I built to replace Joplin because there were some features that I needed that Joplin was missing. 

## Current Version Functions

	  - User accounts with passwords
	  - Create Notebooks
	  - Create Notes inside notebooks
	  - Hide Notebooks and Notes
	  - Option to encrypt hidden items with configurable key
	  - Configurable admin password to recover user passwords if forgotten
	  - Configurable user pin to show hidden items
	  - Notebooks and Notes are sharable (creates a temp web page with unique addresses)	
	  - Links to notebooks or notes in notes
	  - Edit in Markup, View in Rich Text
	  - Mobile and Desktop optimized views
	  - Hide-able shortcut buttons for Indent, Outdent, Bullet Point, and Check Box
	  - Configurable scroll speed
	  - Configurable Quick Save Button
	  - Configurable Auto save with frequency
	  - Configurable Auto Continue Bullet Point and Check Box
	  - Configurable Web UI Colors
	  - Configurable Notebook and Note display order
	  - Live search-able Notebook and Note names
	  - Configurable TLS and self signed certificates
	  - Configurable Speech to Text (see future plans)
	  
	      
	  - Python API Back End
	  - Clean Web Interface
	  - GUI Launch-able
	  - Headless Launch-able
	  - Docker Launch-able

## Future Plans (Already being worked on)

	- Local file sync (per logged in user)
	- Notebook and notes colab option with other users
	- Godot built 2D interface for Linux, Windows, Mac, Android, IOS
	- Godot built 2D and VR/AR interface for Meta Quest
	- Push to talk speech to text function for VR interface

## Server Setup

(A) GUI
	Download the zip file and extract to a folder. Open the run.sh / run.vc in a text editor and choose the option you need to launch with, and save. Run the launch script, verify working directory, verify IP (leave at 0.0.0.0 to accept all incoming IP's in that subdomain), verify port number, and click "Start Server". Finally navigate in a browser to "https://<your pc running server ip>:<desired port>"

(B) GUI - Headless
	Download the zip file and extract to a folder. Open the run.sh / run.vc in a text editor and choose the option you need to launch with, and save, making sure to set headless to 1. Run the launch script. Finally navigate in a browser to "https://<your pc running server ip>:<desired port>"

(C) Docker
	I will update with more details on the docker compose details, but if you try before then, understand that you still need to put at least the HTML and PY files from the zip file into the app directory volume for the docker. These are placed outside the container to make updating easier for me and will eventually be placed inside the docker container.

## UI Options

Currently the UI options outside of the web UI (Tauri/Rust) are automatically routed to my served copy of InkWell. I will eventually change this so they can redirect to a different served copy but this isn't on the immediate road map since I am working on the Godot stand alone interfaces for it which can already be directed to different addresses and will be put here once I am done with the Beta version 
