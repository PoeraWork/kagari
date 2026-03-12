def build_request(context):
    """Build a dynamic ReadDataByIdentifier request based on flow variables."""
    variables = context.get("variables", {})
    did = str(variables.get("did", "F190")).upper()
    return {"request_hex": f"22{did}"}
