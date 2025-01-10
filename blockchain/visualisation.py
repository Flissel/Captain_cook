import networkx as nx
import plotly.graph_objects as go
import json

def clean_and_convert_to_dict(json_string):
    """
    Cleans a JSON-like string by removing unwanted formatting, including `\n`, 
    and converts it into a Python dictionary.

    Args:
        json_string (str): The JSON-like string to clean and parse.

    Returns:
        dict: A cleaned Python dictionary.
    """
    try:
        # Strip the leading/trailing formatting (e.g., ```json and ```).
        cleaned_string = json_string.strip("```json").strip("```").strip()

        # Remove all `\n` characters.
        cleaned_string = cleaned_string.replace("\\n", "").replace("\n", "")

        # Convert the cleaned string to a Python dictionary.
        project_dict = json.loads(cleaned_string)
        return project_dict
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to decode JSON string. Error: {e}")
def restructure_for_tree(project_dict_raw):
    """
    Restructures the project dictionary for tree visualization with departments as top-level nodes.

    Args:
        project_dict (dict): The original project dictionary.

    Returns:
        dict: A restructured dictionary suitable for tree visualization.
    """
    tree_data = {"name": "Project", "children": []}
    project_dict = clean_and_convert_to_dict(project_dict_raw)
    for department in project_dict["overview"]:
        department_node = {"name": department, "children": []}
        
        if department in project_dict["sections"]:
            for subsection, details in project_dict["sections"][department].get("subsections", {}).items():
                subsection_node = {"name": subsection, "children": []}

                # Add tasks as children of subsections
                for task in details["tasks"]:
                    subsection_node["children"].append({"name": task, "type": "task"})

                # Add subsection node to department
                department_node["children"].append(subsection_node)

        # Add department node to the tree
        tree_data["children"].append(department_node)

    return tree_data

def create_project_knowledge_graph(project_structure):
    """
    Creates a knowledge graph for the given project structure.

    Args:
        project_structure (dict): The structured project overview with sections, tasks, and assignments.

    Returns:
        None: Displays the knowledge graph using Plotly.
    """
    project_structure = clean_and_convert_to_dict(project_structure)
    G = nx.DiGraph()

    # Add sections as nodes
    for section, section_data in project_structure["sections"].items():
        G.add_node(section, node_type="section")

        # Add subsections and their tasks
        for subsection, subsection_data in section_data["subsections"].items():
            subsection_node = f"{section} -> {subsection}"
            G.add_node(subsection_node, node_type="subsection")
            G.add_edge(section, subsection_node)

            for task in subsection_data["tasks"]:
                task_node = f"Task: {task}"
                G.add_node(task_node, node_type="task")
                G.add_edge(subsection_node, task_node)

                # Add team and individual assignments
                for team in subsection_data["assignments"]["teams"]:
                    G.add_node(team, node_type="team")
                    G.add_edge(task_node, team)

                for individual in subsection_data["assignments"]["individuals"]:
                    G.add_node(individual, node_type="individual")
                    G.add_edge(task_node, individual)

    # Visualize the graph using Plotly
    pos = nx.spring_layout(G)
    edge_x = []
    edge_y = []

    # Extract edge positions
    for edge in G.edges():
        x0, y0 = pos[edge[0]]
        x1, y1 = pos[edge[1]]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])

    # Create edge traces
    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        line=dict(width=1, color="#888"),
        hoverinfo="none",
        mode="lines"
    )

    # Create node traces
    node_x = []
    node_y = []
    node_text = []
    node_colors = []

    for node in G.nodes():
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)

        node_type = G.nodes[node].get("node_type", "default")
        node_colors.append(
            {"section": "blue", "subsection": "green", "task": "orange", "team": "purple", "individual": "red"}.get(
                node_type, "gray"
            )
        )
        node_text.append(node)

    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=node_text,
        marker=dict(
            size=10,
            color=node_colors,
            line_width=2
        ),
        textposition="top center"
    )

    # Display the graph
    fig = go.Figure(data=[edge_trace, node_trace],
                    layout=go.Layout(
                        title="Project Knowledge Graph",
                        showlegend=False,
                        hovermode="closest",
                        margin=dict(b=0, l=0, r=0, t=40),
                        xaxis=dict(showgrid=False, zeroline=False),
                        yaxis=dict(showgrid=False, zeroline=False)
                    ))
    fig.show()





def plot_project_tree(tree_data):
    """
    Plots an improved hierarchical tree for project organization.

    Args:
        tree_data (dict): The restructured tree data.
    """
    def add_nodes_edges(data, parent_name="", level=0):
        # Add current node
        nodes.append(data["name"])
        levels.append(level)
        if parent_name:
            edges.append((parent_name, data["name"]))
        
        # Recursively add child nodes
        for child in data.get("children", []):
            add_nodes_edges(child, data["name"], level + 1)

    # Initialize nodes and edges
    nodes = []
    edges = []
    levels = []
    add_nodes_edges(tree_data)

    # Map nodes to positions
    node_indices = {node: idx for idx, node in enumerate(nodes)}
    node_x = [idx for idx, level in enumerate(levels)]
    node_y = [-level for level in levels]

    # Create edge traces
    edge_x = []
    edge_y = []
    for edge in edges:
        x0, y0 = node_x[node_indices[edge[0]]], node_y[node_indices[edge[0]]]
        x1, y1 = node_x[node_indices[edge[1]]], node_y[node_indices[edge[1]]]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        line=dict(width=1, color="#888"),
        mode="lines",
        hoverinfo="none"
    )

    # Create node traces with custom colors for levels
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=nodes,
        textposition="top center",
        marker=dict(
            size=10,
            color=[f"rgb({level*40}, {200-level*40}, 150)" for level in levels],
            line=dict(width=2, color="darkblue")
        )
    )

    # Create the figure
    fig = go.Figure(
        data=[edge_trace, node_trace],
        layout=go.Layout(
            title="Improved Project Organization Tree",
            titlefont_size=16,
            showlegend=False,
            hovermode="closest",
            margin=dict(b=0, l=0, r=0, t=40),
            xaxis=dict(showgrid=False, zeroline=False),
            yaxis=dict(showgrid=False, zeroline=False),
        )
    )
    fig.show()
