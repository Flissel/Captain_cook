Impromptu Google Meet Meeting - October 12
VIEW RECORDING: https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8
Meeting Purpose

Onboard Muttu and Arjun to the Captain Cook project architecture.

Key Takeaways

  - Captain Cook Architecture: The project uses Cloud Code to generate Autogen agents, which rely on the MCP Docker Toolkit for tools (e.g., Playwright, SQL) not available in NNN.
  - Critical Docker CLI Issue: Concurrent calls to the MCP Docker Toolkit CLI break Docker. A semaphore is required to serialize calls and prevent deadlocks.
  - "Lost in the Middle" LLM Problem: LLMs lose context in long prompts. The solution is to slice large descriptions into smaller, sequential tasks for agents.
  - New Project Management Tool: "Robot" is a new AI-powered app for project management, designed to provide a central "brain" for tasks, roadmaps, and data analysis.

Topics

Captain Cook Architecture

  - Goal: Use Cloud Code to automate the generation of Autogen agents.
  - Core Components:
      - Cloud Code: The primary tool for generating agent code.
      - Autogen: The framework for the generated multi-agent systems.
      - MCP Docker Toolkit: A container providing tools (e.g., Playwright, SQL) not available in NNN.
      - NNN: A containerized environment where agents execute.
      - Mini-book / MariaDB: The database for storing project status and agent outputs.
  - Agent Output Structure:
      - output/ folder contains agent teams.
      - Each team has a YAML spec, MCP server config, source/main.py, and basic compilation tests.
  - Future Vision: Evolve from Autogen to higher-level frameworks like Open-source or Hermit agents, using Autogen core only as an event stream.

Technical Challenges & Solutions

  - Docker CLI Concurrency:
      - Problem: Multiple agents calling the MCP Docker Toolkit CLI simultaneously causes Docker to break.
      - Solution: Implement a semaphore to serialize calls, ensuring only one agent accesses the CLI at a time.
  - "Lost in the Middle" LLM Problem:
      - Problem: LLMs lose context in the middle of long prompts, leading to inaccurate outputs.
      - Solution: Slice large descriptions into smaller, sequential tasks. Muttu also suggested summarizing previous chat history into a new prompt to maintain context.
  - Hardware Requirements:
      - MacBooks: Preferred for local model development due to their shared memory architecture, which enables clustering for increased power.

Project Management & Strategy

  - "Rowboat" Project Management App:
      - A new AI-powered app for project organization.
      - Key Feature: A central "brain" connecting all project files, emails, and meetings.
      - Goal: Provide a single source of truth for roadmaps, to-do lists, and data analysis.
  - AI Consulting Initiative:
      - A new business line to generate revenue.
      - A larger group meeting will be scheduled to develop the strategy.
  - Sales & Networking:
      - Munich Event (Oct 14-15): A networking event to generate leads for the sales agent.
      - India Expansion: Partnering with "The Wild Mind" to establish a presence in the Indian market.

Next Steps

  - Felix:
      - Push the current project setup to the repo.
      - Schedule the AI consulting strategy meeting with Muttu, Suya, and Bora.
  - Muttu:
      - Use Cloud Code to guide the project setup.
      - Find a time for the AI consulting strategy meeting.
  - Arjun:
      - Schedule a 30-min session with Felix to learn Mermaid diagrams.
      - Use the 7-day Cloud Code trial to begin "wipe coding."
Action Items
  - Schedule case-studies session w/ Sophie, Arjun, Muttu - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=247.9999
  - Push current setup to repo; share files/links w/ Muttu (Robot repo, Mermaid diagrams) - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=1947.9999
  - Use Cloud Code to set up Captain Cook; follow Felix's prompt - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=1958.9999
  - Translate sales agent to English; update calendar/contacts; share w/ Arjun, Muttu - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=2105.9999
  - Schedule 30-min Mermaid diagrams walkthrough w/ Arjun - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=2736.9999
  - Schedule AI consulting kickoff w/ Bora, Suya, Arjun, Muttu - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=2748.9999
  - Create project charter w/ deadlines and owners - WATCH (5 secs): https://fathom.video/share/oWz4zC4Aj5pkQs6xET1mGxsPySz9_jT8?timestamp=2869.9999
