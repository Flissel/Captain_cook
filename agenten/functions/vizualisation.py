import plotly.graph_objects as go
import json

def load_subtasks(file_path="subtasks.json"):
    """Load subtasks from subtasks.json."""
    with open(file_path, "r") as f:
        return json.load(f)["subtasks"]

def load_blockchain(file_path="blockchain.json"):
    """Load blockchain from blockchain.json."""
    with open(file_path, "r") as f:
        return json.load(f)

def visualize_combined_graph(graph):
    """
    Visualizes the combined graph of subtasks and blockchain dependencies.

    Args:
        graph (nx.DiGraph): Combined directed graph.
    """
    pos = nx.spring_layout(graph)

    # Extract edges
    edge_x = []
    edge_y = []
    for edge in graph.edges():
        x0, y0 = pos[edge[0]]
        x1, y1 = pos[edge[1]]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])

    # Extract nodes
    node_x = []
    node_y = []
    node_customdata = []
    for node, data in graph.nodes(data=True):
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)
        node_customdata.append(
            f"<b>{data['title']}</b><br>"
            f"Description: {data['description']}<br>"
            f"Priority: {data['priority']}"
        )

    # Create edge traces
    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        line=dict(width=0.5, color="#888"),
        hoverinfo="none",
        mode="lines"
    )

    # Create node traces
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        hoverinfo="text",
        marker=dict(
            size=20,
            color="#1f77b4",
            line_width=2
        ),
        customdata=node_customdata,
        hovertemplate="%{customdata}<extra></extra>"
    )

    # Create figure
    fig = go.Figure(data=[edge_trace, node_trace],
                    layout=go.Layout(
                        title="Task Tree with Dependencies",
                        titlefont_size=16,
                        showlegend=False,
                        hovermode="closest",
                        margin=dict(b=0, l=0, r=0, t=40),
                        xaxis=dict(showgrid=False, zeroline=False),
                        yaxis=dict(showgrid=False, zeroline=False)
                    ))
    fig.show()
import networkx as nx

def combine_task_and_blockchain_data(subtasks, blockchain):
    """
    Combines subtasks (from subtasks.json) and dependencies (from blockchain.json) into a graph.

    Args:
        subtasks (list): List of subtasks from subtasks.json.
        blockchain (list): Blockchain data from blockchain.json.

    Returns:
        nx.DiGraph: Combined directed graph.
    """
    graph = nx.DiGraph()

    # Add nodes for subtasks
    for subtask in subtasks:
        graph.add_node(
            subtask["subtask_number"],
            title=subtask["title"],
            description=subtask["description"],
            priority=subtask["priority"]
        )

    # Add edges for subtask dependencies
    for subtask in subtasks:
        for dependency in subtask["dependencies"]:
            dep_num = next(
                (st["subtask_number"] for st in subtasks if st["title"] == dependency), None
            )
            if dep_num:
                graph.add_edge(dep_num, subtask["subtask_number"])

    # Add edges from blockchain for task dependencies
    for block in blockchain:
        for child_index in block.get("children", []):
            graph.add_edge(block["index"], child_index)

    return graph
