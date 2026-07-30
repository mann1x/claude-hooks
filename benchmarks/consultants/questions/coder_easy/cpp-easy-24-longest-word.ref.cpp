#include <iostream>
#include <string>
int main() {
    std::string w, best;
    while (std::cin >> w)
        if (w.size() > best.size()) best = w;
    std::cout << best << std::endl;
    return 0;
}
