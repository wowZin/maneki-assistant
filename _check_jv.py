try:
    import jvQuant
    print("jvQuant imported OK:", jvQuant.__version__)
except ImportError as e:
    print("FAILED:", e)
