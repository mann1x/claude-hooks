#include <iostream>
#include <unordered_set>
int main() {
    long long x;
    std::unordered_set<long long> seen;
    bool first = true;
    while (std::cin >> x) {
        if (seen.insert(x).second) {
            if (!first) std::cout << ' ';
            std::cout << x;
            first = false;
        }
    }
    std::cout << std::endl;
    return 0;
}
