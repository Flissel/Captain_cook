import networkx as nx
import plotly.graph_objects as go

def generate_interactive_subtask_graph(subtasks):
    """
    Generates an interactive dependency graph for the provided subtasks using Plotly.

    Args:
        subtasks (list): List of subtasks, each represented as a dictionary with keys
                         'subtask_number', 'title', 'dependencies', etc.
    """
    # Create a directed graph
    graph = nx.DiGraph()

    # Add nodes and edges based on dependencies
    for subtask in subtasks:
        # Add the node with its attributes
        graph.add_node(subtask["subtask_number"], title=subtask["title"], description=subtask["description"], priority=subtask["priority"])
        for dependency in subtask["dependencies"]:
            # Find the subtask number for the dependency
            dep_num = next(
                (st["subtask_number"] for st in subtasks if st["title"] == dependency), None
            )
            if dep_num:
                graph.add_edge(dep_num, subtask["subtask_number"])

    # Generate positions for the graph layout
    pos = nx.spring_layout(graph)

    # Extract data for Plotly
    edge_x = []
    edge_y = []
    for edge in graph.edges():
        x0, y0 = pos[edge[0]]
        x1, y1 = pos[edge[1]]
        edge_x.append(x0)
        edge_x.append(x1)
        edge_x.append(None)
        edge_y.append(y0)
        edge_y.append(y1)
        edge_y.append(None)

    node_x = []
    node_y = []
    node_info = []
    for node, data in graph.nodes(data=True):
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)
        # Tooltip with subtask details
        node_info.append(
            f"<b>{data['title']}</b><br>"
            f"Description: {data['description']}<br>"
            f"Priority: {data['priority']}"
        )

    # Create the edge traces
    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        line=dict(width=0.5, color="#888"),
        hoverinfo="none",
        mode="lines"
    )

    # Create the node traces
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        hoverinfo="text",
        marker=dict(
            size=20,
            color="#1f77b4",  # Blue color for nodes
            line_width=2
        ),
        text=[str(node) for node in graph.nodes()],  # Subtask numbers as labels
        textposition="top center",
        customdata=node_info  # Subtask details
    )

    # Create the figure
    fig = go.Figure(data=[edge_trace, node_trace],
                    layout=go.Layout(
                        title="Interactive Subtask Dependency Graph",
                        titlefont_size=16,
                        showlegend=False,
                        hovermode="closest",
                        margin=dict(b=0, l=0, r=0, t=40),
                        xaxis=dict(showgrid=False, zeroline=False),
                        yaxis=dict(showgrid=False, zeroline=False)
                    ))

    # Show the interactive plot
    fig.show()
